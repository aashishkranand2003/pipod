"""
Music Player — Raspberry Pi Zero 2W
Display : 3.5-inch SPI framebuffer (/dev/fb1, 320×480, RGB565)
Audio   : USB audio output (ALSA card index auto-detected, falls back to card 1)
"""

import io
import math
import os
import sys
import time
import glob as _glob
import fcntl as _fcntl
import struct as _struct
import threading
import subprocess
import numpy as np
import pygame
from pygame.locals import QUIT
import requests
try:
    from evdev import InputDevice, ecodes, list_devices  # type: ignore[reportMissingImports]
    EVDEV_AVAILABLE = True
except Exception:
    InputDevice = None
    ecodes = None
    list_devices = None
    EVDEV_AVAILABLE = False
from ytmusicapi import YTMusic
import yt_dlp
import vlc

# ══════════════════════════════════════════════════════════════
# DISPLAY — use the raw framebuffer, no X11 / Wayland needed
# ══════════════════════════════════════════════════════════════
os.environ["SDL_VIDEODRIVER"] = "dummy"
# Force pygame to use a null audio driver so it never opens ALSA/PulseAudio.
# All audio is handled exclusively by VLC → USB DAC; pygame must not compete.
os.environ["SDL_AUDIODRIVER"] = "dummy"
_fb = open("/dev/fb1", "wb")

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
SCREEN_W       = 320
SCREEN_H       = 480
MAX_HISTORY    = 100
FETCH_COOLDOWN = 7.0           # seconds between auto-fetch calls
FPS            = 30

# ══════════════════════════════════════════════════════════════
# AUDIO OUTPUT — Smart Switching (USB DAC ↔ Bluetooth)
# ══════════════════════════════════════════════════════════════

def _get_pulse_sinks():
    """Return list of available PulseAudio sinks."""
    try:
        result = subprocess.run(["pactl", "list", "short", "sinks"],
                              capture_output=True, text=True, timeout=3)
        return result.stdout.strip().splitlines()
    except:
        return []

def _detect_usb_dac_sink():
    """Find the best USB DAC sink."""
    sinks = _get_pulse_sinks()
    # Prioritize common USB DAC names
    priorities = ["USB", "DAC", "Audio", "Headphones", "Speaker"]
    
    for priority in priorities:
        for line in sinks:
            if priority.lower() in line.lower() and "bluez" not in line.lower():
                sink_name = line.split()[1]
                print(f"✅ USB DAC detected: {sink_name}")
                return sink_name
    # Fallback to first non-BT sink
    for line in sinks:
        if "bluez" not in line.lower():
            sink_name = line.split()[1]
            print(f"Using fallback sink: {sink_name}")
            return sink_name
    return None

def _set_default_sink(sink_name: str):
    """Set PulseAudio default sink and restart VLC."""
    if not sink_name:
        return False
    try:
        subprocess.run(["pactl", "set-default-sink", sink_name],
                      capture_output=True, check=True, timeout=5)
        print(f"🔊 Audio routed to: {sink_name}")
        _vlc_restart_audio()
        return True
    except Exception as e:
        print(f"Failed to set sink {sink_name}: {e}")
        return False

def switch_to_usb_dac():
    """Switch audio back to USB DAC."""
    sink = _detect_usb_dac_sink()
    if sink:
        _set_default_sink(sink)
    else:
        print("⚠ No USB DAC found, using system default")

def switch_to_bluetooth(mac: str):
    """Switch to Bluetooth A2DP sink."""
    return _pa_set_bt_sink(mac)  # reuse your existing robust function

# Improved version of your existing function
def _pa_reset_sink() -> None:
    """Called when Bluetooth disconnects."""
    print("🔄 Bluetooth disconnected → switching back to USB DAC")
    switch_to_usb_dac()

# ══════════════════════════════════════════════════════════════
# PulseAudio Event Monitoring
# ══════════════════════════════════════════════════════════════

def _pa_event_monitor():
    """Background thread that watches for device changes."""
    print("👀 Starting PulseAudio device monitor...")
    try:
        proc = subprocess.Popen(
            ["pactl", "subscribe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        while True:
            line = proc.stdout.readline()
            if not line:
                break
                
            line_lower = line.lower()
            
            # Bluetooth device connected
            if "bluez" in line_lower and ("new" in line_lower or "change" in line_lower):
                with _bt_lock:
                    if bt_connected_mac:
                        print("🔵 BT device change detected → routing to Bluetooth")
                        switch_to_bluetooth(bt_connected_mac)
            
            # Bluetooth device removed/disconnected
            elif "remove" in line_lower and "bluez" in line_lower:
                print("🔵 Bluetooth device removed")
                _pa_reset_sink()
                
    except Exception as e:
        print(f"PA monitor error: {e}")

# Start the monitor in background
_monitor_thread = threading.Thread(target=_pa_event_monitor, daemon=True)
_monitor_thread.start()


# ══════════════════════════════════════════════════════════════
# VLC — force output to the USB audio device
# ══════════════════════════════════════════════════════════════
def _detect_audio_output():
    """
    Detect which audio backend to use.
    Priority: PulseAudio (if daemon running) → ALSA.
    Returns (aout_arg, sink_name_or_device).
    """
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            # PA is running — find the default sink
            for line in r.stdout.splitlines():
                if "Default Sink:" in line:
                    sink = line.split(":", 1)[1].strip()
                    print(f"Audio: PulseAudio, default sink = {sink}")
                    return ("pulse", sink)
            print("Audio: PulseAudio running, sink unknown")
            return ("pulse", None)
    except Exception:
        pass
    print("Audio: PulseAudio not available, using ALSA")
    return ("alsa", None)

_aout_backend, _ = _detect_audio_output()

# Print all available PA sinks at startup for diagnostics
try:
    _sink_list = subprocess.run(["pactl", "list", "short", "sinks"],
        capture_output=True, text=True, timeout=3).stdout.strip()
    print(f"Available PA sinks:\n{_sink_list or '  (none)'}")
except Exception:
    pass

_vlc_args = [
    "--no-video",
    "--quiet",
    "--no-ts-trust-pcr",
    f"--aout={_aout_backend}",
    "--no-sout-keep",
    "--network-caching=3000",
    "--live-caching=3000",
    "--audio-resampler=soxr",
    "--clock-jitter=0",
    "--clock-synchro=0",
]
vlc_instance = vlc.Instance(*_vlc_args)
player       = vlc_instance.media_player_new()

def _vlc_restart_audio() -> None:
    """Stop and restart VLC audio output so it picks up the new PA default sink."""
    try:
        player.audio_output_set(_aout_backend)
    except Exception as e:
        print(f"VLC audio restart: {e}")

# ══════════════════════════════════════════════════════════════
# PYGAME SETUP
# ══════════════════════════════════════════════════════════════
pygame.init()
screen = pygame.Surface((SCREEN_W, SCREEN_H))
pygame.display.set_caption("Music Player")

# ── Colours ──────────────────────────────────────────────────
BG_COLOR       = ( 70,  40, 120)
BG_SECONDARY   = (110,  60, 190)
TEXT_COLOR     = (255, 255, 255)
TEXT_DIM       = (200, 200, 220)
PROGRESS_COLOR = (220, 220, 255)
SEARCH_BG      = (255, 255, 255)
SEARCH_TEXT    = (  0,   0,   0)
KEY_BG         = ( 72,  52, 130)   # regular letter key – deep violet
KEY_BG_LIGHT   = ( 95,  72, 165)   # top-highlight strip on regular key
KEY_SPECIAL    = (180,  48,  48)   # BKSP/DEL – red
KEY_SPECIAL_LT = (210,  70,  70)   # highlight strip for BKSP
KEY_SEARCH     = ( 34, 148,  84)   # SEARCH – green
KEY_SEARCH_LT  = ( 52, 185, 110)   # highlight for SEARCH
KEY_SPACE_BG   = ( 55,  38, 110)   # SPACE – darker than letters
KEY_SPACE_LT   = ( 78,  58, 148)   # highlight for SPACE
CLOSE_COLOR    = (200,  40,  40)   # keyboard CLOSE button
KB_SHADOW      = ( 15,   8,  35)   # drop-shadow tint under whole keyboard

# ── Fonts ────────────────────────────────────────────────────
font_title  = pygame.font.SysFont("arial", 20, bold=True)
font_artist = pygame.font.SysFont("arial", 17)
font_time   = pygame.font.SysFont("arial", 14)
font_ctrl   = pygame.font.SysFont("arial", 26)
font_search = pygame.font.SysFont("arial", 18)
font_small  = pygame.font.SysFont("arial", 13)
font_key    = pygame.font.SysFont("arial", 16, bold=True)

# ── YTMusic ──────────────────────────────────────────────────
ytmusic = YTMusic()

# ══════════════════════════════════════════════════════════════
# LAYOUT  (all rects defined once)
# ══════════════════════════════════════════════════════════════
SEARCH_H       = 48
THUMBNAIL_SIZE = 240
# Two icon buttons right of the search bar: [BT] [WiFi]
# Each button is 32 px wide with 4 px gap; search bar shrinks accordingly.
BT_BTN_RECT   = pygame.Rect(SCREEN_W - 74, 8, 32, 36)    # Bluetooth icon button
WIFI_BTN_RECT = pygame.Rect(SCREEN_W - 38, 8, 32, 36)    # WiFi icon button
SEARCH_RECT   = pygame.Rect(10, 8, SCREEN_W - 86, 36)    # narrowed to fit both buttons
CLEAR_RECT    = pygame.Rect(SCREEN_W - 88, 10, 10, 32)   # (legacy; hit by SEARCH_RECT.inflate)
THUMBNAIL_RECT = pygame.Rect((SCREEN_W - THUMBNAIL_SIZE) // 2,
                              SEARCH_H + 10, THUMBNAIL_SIZE, THUMBNAIL_SIZE)
TITLE_RECT     = pygame.Rect(12, THUMBNAIL_RECT.bottom + 8,  SCREEN_W - 24, 28)
ARTIST_RECT    = pygame.Rect(12, TITLE_RECT.bottom + 1,      SCREEN_W - 24, 22)
SEEK_RECT      = pygame.Rect(12, ARTIST_RECT.bottom + 8,     SCREEN_W - 24,  8)
TIME_RECT      = pygame.Rect(12, SEEK_RECT.bottom + 3,       SCREEN_W - 24, 20)
VOL_RECT       = pygame.Rect(12, TIME_RECT.bottom + 3,       SCREEN_W - 24, 12)
VOL_BAR_RECT   = pygame.Rect(VOL_RECT.x + 35, VOL_RECT.y,
                              VOL_RECT.width - 40, VOL_RECT.height)
CONTROLS_RECT  = pygame.Rect(12, VOL_RECT.bottom + 3,        SCREEN_W - 24, 64)

_BTN  = 64
_SP   = (CONTROLS_RECT.width - _BTN * 3) // 4
PREV_RECT = pygame.Rect(CONTROLS_RECT.x + _SP,          CONTROLS_RECT.y, _BTN, _BTN)
PLAY_RECT = pygame.Rect(PREV_RECT.right + _SP,           CONTROLS_RECT.y, _BTN, _BTN)
NEXT_RECT = pygame.Rect(PLAY_RECT.right + _SP,           CONTROLS_RECT.y, _BTN, _BTN)

# ══════════════════════════════════════════════════════════════
# KEYBOARD LAYOUT
# ══════════════════════════════════════════════════════════════
# ── Keyboard rows  (order matters – row index drives y position) ─────────────
# Row 3 has 7 letters + BKSP.  BKSP gets 1.5× a letter-key width so it is
# easy to tap and clearly distinct.
# Row 4 has SPACE (wide) + SEARCH (medium).
QWERTY_ROWS_ALPHA = [
    ["q","w","e","r","t","y","u","i","o","p"],
    ["a","s","d","f","g","h","j","k","l"],
    ["z","x","c","v","b","n","m","BKSP"],
    ["?123","SPACE","SEARCH"],
]

QWERTY_ROWS_SYM = [
    ["1","2","3","4","5","6","7","8","9","0"],
    ["!","@","#","$","%","^","&","*","(",")",],
    ["-","_","=","+","[","]","{","}",";","BKSP"],
    ["ABC","SPACE","SEARCH"],
]

QWERTY_ROWS = QWERTY_ROWS_ALPHA   # active layout (toggled at runtime)

KB_MARGIN  = 4          # left/right edge gap (px)
KB_PAD     = 3          # gap between keys (px)
KB_KEY_H   = 42         # key height (px)
# Keyboard anchored to the bottom: 3 alpha rows + 1 bottom row + preview bar
# Total KB height = 4 rows * (KB_KEY_H + KB_PAD) + preview_bar(48) + bottom_pad(4)
_KB_N_ROWS     = 4
_KB_TOTAL_H    = _KB_N_ROWS * (KB_KEY_H + KB_PAD) + 48 + 4
KB_TOP         = SCREEN_H - _KB_TOTAL_H + 48 + 4   # y of row-0 keys
KB_W       = SCREEN_W - KB_MARGIN * 2   # usable keyboard width
kb_sym_mode    = False   # False = alpha layout, True = symbol layout

# ══════════════════════════════════════════════════════════════
# TOUCH CALIBRATION  (XPT2046 / ADS7846)
# Values loaded from ~/touch_conf.json first (manual override),
# then ~/touch_cal.json (generated by calibrate.py), else defaults.
# ══════════════════════════════════════════════════════════════
import json as _json
_CONF_PATH = os.path.expanduser("~/touch_conf.json")
_CAL_PATH = os.path.expanduser("~/touch_cal.json")


def _extract_cal_bounds(cal_obj):
    req = ("TOUCH_X_MIN", "TOUCH_X_MAX", "TOUCH_Y_MIN", "TOUCH_Y_MAX")
    if all(k in cal_obj for k in req):
        return cal_obj
    nested = cal_obj.get("bounds") if isinstance(cal_obj, dict) else None
    if isinstance(nested, dict) and all(k in nested for k in req):
        return nested
    raise KeyError("Missing touch calibration bounds")


TOUCH_X_MIN = 213
TOUCH_X_MAX = 3884
TOUCH_Y_MIN = 733
TOUCH_Y_MAX = 3826

_loaded = False
for _path in (_CONF_PATH, _CAL_PATH):
    try:
        with open(_path) as _cf:
            _cal = _json.load(_cf)
        _bounds = _extract_cal_bounds(_cal)
        _x_min = int(_bounds["TOUCH_X_MIN"])
        _x_max = int(_bounds["TOUCH_X_MAX"])
        _y_min = int(_bounds["TOUCH_Y_MIN"])
        _y_max = int(_bounds["TOUCH_Y_MAX"])
        if _x_min >= _x_max or _y_min >= _y_max:
            raise ValueError("Invalid touch calibration range")

        TOUCH_X_MIN = _x_min
        TOUCH_X_MAX = _x_max
        TOUCH_Y_MIN = _y_min
        TOUCH_Y_MAX = _y_max
        _loaded = True
        print(f"Touch calibration loaded from {_path}")
        print(f"  X: {TOUCH_X_MIN}-{TOUCH_X_MAX}  Y: {TOUCH_Y_MIN}-{TOUCH_Y_MAX}")
        if isinstance(_cal, dict) and "SAMPLES_PER_TARGET" in _cal:
            print(f"  Samples/target: {_cal['SAMPLES_PER_TARGET']}")
        break
    except FileNotFoundError:
        continue
    except Exception as _cal_err:
        print(f"Touch calibration load failed from {_path}: {_cal_err}")

if not _loaded:
    print(
        f"Touch config not found ({_CONF_PATH}, {_CAL_PATH}) -- using defaults"
    )
TOUCH_KEYWORDS = ["xpt2046", "ads7846", "ft5", "goodix", "touch", "pen"]

# ══════════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════════
current_song    = None
playlist        = []
history         = []
is_playing      = False
current_time    = 0.0
duration        = 0.0
search_text     = ""
thumbnail_surf  = None
seen_video_ids  = set()
last_fetch_time = 0.0
loading         = False
loading_message = ""
kb_active       = False
kb_sym_mode     = False   # symbol layer toggle
typed_query     = ""
current_volume  = 25
kb_rects: dict  = {}

# ── Bluetooth state ───────────────────────────────────────────
bt_active        = False          # BT screen visible
bt_scanning      = False          # discovery in progress
bt_power         = False          # adapter powered on (set True after startup init)
bt_devices: list = []             # [{mac, name, type, connected, paired}]
bt_connected_mac = None           # currently connected device MAC
bt_screen_rects: dict = {}        # hit-rects for BT screen widgets
bt_scroll        = 0              # device list scroll offset (Available tab)
bt_saved_scroll  = 0              # device list scroll offset (Saved tab)
bt_tab           = "available"    # active tab: "available" | "saved"
_bt_lock         = threading.Lock()
_bt_scan_thread  = None

# ── Bluetooth pairing confirmation overlay ────────────────────
# When a device requests user confirmation (SSP Just-Works or passkey),
# the pairing worker sets these to trigger the on-screen YES/NO dialog.
bt_confirm_mac    = None   # MAC waiting for confirmation (None = no dialog)
bt_confirm_name   = ""     # human-readable name for the dialog
bt_confirm_pin    = ""     # passkey/PIN to show (empty for Just-Works)
_bt_confirm_event = threading.Event()   # worker blocks on this
_bt_confirm_answer = False              # True = YES, False = NO / timeout

# ── WiFi state ───────────────────────────────────────────────
wifi_active         = False          # WiFi screen visible
wifi_networks: list = []             # [{ssid, signal, security, connected, saved}]
wifi_connected_ssid = None           # SSID currently connected
wifi_scanning       = False          # scan in progress
wifi_screen_rects: dict = {}         # hit-rects for WiFi screen widgets
wifi_scroll         = 0              # scroll offset for available list
wifi_saved_scroll   = 0              # scroll offset for saved list
wifi_tab            = "available"    # active tab: "available" | "saved"
_wifi_lock          = threading.Lock()
# Password entry overlay
wifi_pw_ssid        = None           # SSID waiting for password (None = no overlay)
wifi_pw_text        = ""             # current password being typed
wifi_pw_security    = ""             # security type of target network
_wifi_pw_kb_rects: dict = {}         # hit-rects for password keyboard

# ── Tap debug overlay ─────────────────────────────────────────
# Set True to draw a red dot + coordinates at every tap position.
# Useful for tuning TOUCH_X/Y_MIN/MAX and margin values.
# Set False once calibration is correct.
TAP_DEBUG   = False
_tap_dots   = []    # list of (x, y, expire_monotonic)
TAP_DOT_TTL = 1.0   # seconds each dot stays visible

# ── Screen sleep state ────────────────────────────────────────
screen_on            = True    # False = display blanked
_last_thumb_tap_time = 0.0     # monotonic time of previous tap on thumbnail
DOUBLE_TAP_MAX_GAP   = 0.45    # seconds — window for a valid double-tap
_last_wake_tap_time  = 0.0     # separate timer used only for wake-from-sleep
_black_fb            = b"\x00" * (SCREEN_W * SCREEN_H * 2)  # pre-built blank frame

# ── Pre-computed gradient (drawn once, reused every frame) ───
_gradient_surf = pygame.Surface((SCREEN_W, SCREEN_H))
for _y in range(SCREEN_H):
    _t = _y / SCREEN_H
    _r = int(BG_COLOR[0] + (BG_SECONDARY[0] - BG_COLOR[0]) * _t)
    _g = int(BG_COLOR[1] + (BG_SECONDARY[1] - BG_COLOR[1]) * _t)
    _b = int(BG_COLOR[2] + (BG_SECONDARY[2] - BG_COLOR[2]) * _t)
    pygame.draw.line(_gradient_surf, (_r, _g, _b), (0, _y), (SCREEN_W, _y))

# ══════════════════════════════════════════════════════════════
# STREAM PRE-FETCH CACHE
# ══════════════════════════════════════════════════════════════
_stream_cache       : dict = {}
_stream_cache_lock  = threading.Lock()
_prefetch_in_flight : set  = set()
_prefetch_lock      = threading.Lock()   # guards _prefetch_in_flight

_YDL_OPTS = {
    # Prefer m4a (AAC) — more universally decoded cleanly by VLC/ALSA on Pi.
    # opus/webm (the default "bestaudio") can cause glitches on some USB DACs.
    "format"         : "bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio/best",
    "quiet"          : True,
    "no_warnings"    : True,
    "extractor_args" : {"youtube": {"player_client": ["android"]}},
}

def _prefetch_worker(video_id: str) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
            info   = ydl.extract_info(url, download=False)
            stream = info.get("url")
        if stream:
            with _stream_cache_lock:
                _stream_cache[video_id] = stream
            print(f"✅ Pre-cached stream for {video_id}")
        else:
            print(f"⚠ No stream URL in pre-fetch for {video_id}")
    except Exception as e:
        print(f"⚠ Pre-fetch failed for {video_id}: {e}")
    finally:
        with _prefetch_lock:
            _prefetch_in_flight.discard(video_id)

def prefetch_stream(video_id: str) -> None:
    """Kick off background URL fetch if not already cached or in-flight."""
    with _stream_cache_lock:
        if video_id in _stream_cache:
            return
    with _prefetch_lock:
        if video_id in _prefetch_in_flight:
            return
        _prefetch_in_flight.add(video_id)
    threading.Thread(target=_prefetch_worker, args=(video_id,), daemon=True).start()

def get_stream_url(video_id: str):
    """Return cached URL instantly, or block-fetch as fallback."""
    with _stream_cache_lock:
        cached = _stream_cache.pop(video_id, None)
    if cached:
        print(f"⚡ Using pre-cached stream for {video_id}")
        return cached
    print(f"🔄 Cache miss — fetching stream for {video_id}")
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("url")
    except Exception as e:
        print("get_stream_url failed:", e)
        return None

# ══════════════════════════════════════════════════════════════
# TOUCH
# ══════════════════════════════════════════════════════════════
def find_touch_device():
    if not EVDEV_AVAILABLE:
        print("Touch input disabled (evdev not available)")
        return None
    for path in list_devices():
        try:
            dev = InputDevice(path)
            if any(kw in dev.name.lower() for kw in TOUCH_KEYWORDS):
                print(f"Touch device: {dev.name} ({path})")
                return dev
        except Exception:
            continue
    for fallback in ["/dev/input/event1", "/dev/input/event0"]:
        try:
            dev = InputDevice(fallback)
            print(f"Fallback touch: {dev.name} ({fallback})")
            return dev
        except Exception:
            continue
    return None

touch_dev = find_touch_device()

touch_state = {
    "x": 0, "y": 0, "mt_x": 0, "mt_y": 0,
    "touching": False, "just_pressed": False,
    "tap_x": 0, "tap_y": 0,
}

_use_mt = False
if touch_dev:
    _caps      = touch_dev.capabilities()
    _abs_codes = _caps.get(ecodes.EV_ABS, [])
    _flat      = [c[0] if isinstance(c, tuple) else c for c in _abs_codes]
    if ecodes.ABS_MT_POSITION_X in _flat:
        _use_mt = True
        print("Touch mode: ABS_MT (multitouch)")
    else:
        print("Touch mode: ABS (single-touch)")

def transform_touch(raw_x: int, raw_y: int):
    raw_x = max(TOUCH_X_MIN, min(TOUCH_X_MAX, raw_x))
    raw_y = max(TOUCH_Y_MIN, min(TOUCH_Y_MAX, raw_y))
    
    nx = (raw_x - TOUCH_X_MIN) / (TOUCH_X_MAX - TOUCH_X_MIN)
    ny = (raw_y - TOUCH_Y_MIN) / (TOUCH_Y_MAX - TOUCH_Y_MIN)

    rx = (1.0 - ny) * SCREEN_W
    ry = ((1.0 - nx) * SCREEN_H)

    sx = max(0, min(SCREEN_W - 1, round(rx)))
    sy = max(0, min(SCREEN_H - 1, round(ry)))
    
    return sx, sy
# ══════════════════════════════════════════════════════════════
# FRAMEBUFFER WRITE  (RGB565, landscape→portrait rotation)
# ══════════════════════════════════════════════════════════════
def surface_to_fb(surf: pygame.Surface) -> None:
    rotated = pygame.transform.rotate(surf, -90)
    rotated = pygame.transform.flip(rotated, True, False)
    raw = pygame.surfarray.array3d(rotated).astype(np.uint16)
    rgb565 = ((raw[:, :, 0] >> 3) << 11) | \
             ((raw[:, :, 1] >> 2) <<  5) | \
              (raw[:, :, 2] >> 3)
    _fb.seek(0)
    _fb.write(rgb565.astype("<u2").tobytes())
    _fb.flush()

# ══════════════════════════════════════════════════════════════
# BACKLIGHT CONTROL
#
# 3.5-inch SPI displays (ILI9486 / XPT2046) expose their backlight
# through one of three interfaces, tried in priority order:
#
#   1. /sys/class/backlight/*/brightness   <- preferred (driver-managed)
#   2. /sys/class/gpio sysfs               <- raw GPIO (common pin: 18)
#   3. FBIOBLANK ioctl on /dev/fb1         <- last resort, software only
#
# Common backlight GPIO pins: 18 (MHS/Waveshare), 24 (Kuman/LovyanGFX).
# Adjust BACKLIGHT_GPIO below if your display uses a different pin.
# ══════════════════════════════════════════════════════════════
BACKLIGHT_GPIO = 18   # <- change to match your display hat wiring

def _sysfs_write(path: str, value: str) -> bool:
    """Write value to a sysfs path; return True on success."""
    try:
        with open(path, "w") as fh:
            fh.write(value)
        return True
    except OSError:
        return False

def _find_backlight_sysfs():
    """Return the brightness sysfs path for the first backlight device, or None."""
    hits = _glob.glob("/sys/class/backlight/*/brightness")
    return hits[0] if hits else None

def _gpio_export(pin: int) -> None:
    direction = f"/sys/class/gpio/gpio{pin}/direction"
    if not os.path.exists(direction):
        _sysfs_write("/sys/class/gpio/export", str(pin))
        time.sleep(0.05)          # let udev settle
    _sysfs_write(direction, "out")

def _set_backlight(on: bool) -> None:
    """
    Turn the SPI display backlight on or off.
    Tries sysfs backlight driver -> GPIO sysfs -> FBIOBLANK ioctl.
    """
    # Method 1: kernel backlight driver
    bl = _find_backlight_sysfs()
    if bl:
        max_path = bl.replace("brightness", "max_brightness")
        try:
            with open(max_path) as _f:
                max_val = int(_f.read().strip())
        except Exception:
            max_val = 255
        if _sysfs_write(bl, str(max_val) if on else "0"):
            print(f"Backlight {'ON' if on else 'OFF'} via sysfs ({bl})")
            return

    # Method 2: raw GPIO sysfs
    gpio_val = f"/sys/class/gpio/gpio{BACKLIGHT_GPIO}/value"
    try:
        _gpio_export(BACKLIGHT_GPIO)
        if _sysfs_write(gpio_val, "1" if on else "0"):
            print(f"Backlight {'ON' if on else 'OFF'} via GPIO{BACKLIGHT_GPIO}")
            return
    except Exception as e:
        print(f"GPIO backlight failed: {e}")

    # Method 3: FBIOBLANK ioctl (FBIOBLANK=0 unblank, 1=blank)
    FBIOBLANK = 0x4611
    try:
        with open("/dev/fb1", "wb") as fb_ctl:
            _fcntl.ioctl(fb_ctl, FBIOBLANK, _struct.pack("I", 0 if on else 1))
        print(f"Backlight {'ON' if on else 'OFF'} via FBIOBLANK ioctl")
    except Exception as e:
        print(f"FBIOBLANK failed: {e} — software blank only")

# ══════════════════════════════════════════════════════════════
# SCREEN SLEEP / WAKE
# Blanks the framebuffer AND cuts the physical backlight.
# Audio playback is completely unaffected.
# ══════════════════════════════════════════════════════════════
def screen_sleep() -> None:
    global screen_on
    screen_on = False
    # Write black frame first — no flash when backlight cuts
    _fb.seek(0)
    _fb.write(_black_fb)
    _fb.flush()
    _set_backlight(False)
    print("🌑 Screen off")

def screen_wake() -> None:
    global screen_on, _last_wake_tap_time, _last_thumb_tap_time
    _set_backlight(True)
    screen_on = True      # re-enable rendering before the next frame
    # Reset both timers so the first tap after waking never mis-fires
    _last_wake_tap_time  = 0.0
    _last_thumb_tap_time = 0.0
    print("🌕 Screen on")

def _check_sleep_double_tap() -> bool:
    """Return True if this tap on the thumbnail is a double-tap (triggers sleep).
    Uses its own timer so it is never confused with the wake timer."""
    global _last_thumb_tap_time
    now = time.monotonic()
    gap = now - _last_thumb_tap_time
    _last_thumb_tap_time = now
    if gap <= DOUBLE_TAP_MAX_GAP:
        _last_thumb_tap_time = 0.0   # reset — triple-tap won't re-trigger
        return True
    return False

def _check_wake_double_tap() -> bool:
    """Return True if this tap is the second tap while screen is off (triggers wake).
    Uses a separate timer — sleeping the screen never primes this counter."""
    global _last_wake_tap_time
    now = time.monotonic()
    gap = now - _last_wake_tap_time
    _last_wake_tap_time = now
    if gap <= DOUBLE_TAP_MAX_GAP:
        _last_wake_tap_time = 0.0   # reset after successful wake
        return True
    return False

# ══════════════════════════════════════════════════════════════
# VOLUME  (tries common USB DAC control names in order)
# ══════════════════════════════════════════════════════════════
_VOLUME_CONTROLS = ["Speaker", "PCM", "Master", "Headphone", "Digital"]

def set_usb_volume(percent: int) -> None:
    global current_volume
    current_volume = max(0, min(100, int(percent)))
    for ctrl in _VOLUME_CONTROLS:
        try:
            subprocess.run(
                ["amixer",  "-M", "-q","sset", ctrl, f"{current_volume}%"],
                check=True,
                stderr=subprocess.DEVNULL,
            )
            return          # stop on first success
        except subprocess.CalledProcessError:
            continue

# ══════════════════════════════════════════════════════════════
# BLUETOOTH  (uses bluetoothctl via subprocess — no extra deps)
# ══════════════════════════════════════════════════════════════
_BT_DEVICE_KEYWORDS = {
    "headphones" : ["headphone","headset","earphone","earbuds","airpods",
                     "buds","wh-","wf-","he-","sport"],
    "speaker"    : ["speaker","soundbar","jbl","bose","marshall","sonos",
                     "ultimate","boom","charge","flip","pulse","pill","go "],
    "phone"      : ["phone","iphone","samsung","pixel","huawei","redmi",
                     "poco","oneplus","xiaomi","oppo","realme","motorola"],
    "keyboard"   : ["keyboard","kbd"],
    "mouse"      : ["mouse"],
}

def _bt_classify(name: str) -> str:
    n = name.lower()
    for dev_type, keywords in _BT_DEVICE_KEYWORDS.items():
        if any(k in n for k in keywords):
            return dev_type
    return "device"

def _bt_run(args: list, timeout: int = 4) -> str:
    """Run a bluetoothctl sub-command and return stdout."""
    try:
        r = subprocess.run(
            ["bluetoothctl"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except FileNotFoundError:
        return ""          # bluetoothctl not installed
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        print(f"bluetoothctl {args}: {e}")
        return ""

def _bt_parse_device_list(output: str) -> list:
    devs = []
    for line in output.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device":
            devs.append({"mac": parts[1], "name": parts[2]})
    return devs

def bt_refresh_devices() -> None:
    """Poll bluetoothctl for current adapter + device state (runs in bg thread)."""
    global bt_power, bt_devices, bt_connected_mac

    # Adapter power state
    powered = False
    for line in _bt_run(["show"]).splitlines():
        if "Powered:" in line:
            powered = "yes" in line.lower()
            break
    with _bt_lock:
        bt_power = powered

    if not powered:
        with _bt_lock:
            bt_devices = []
            bt_connected_mac = None
        return

    # All known devices
    all_devs   = _bt_parse_device_list(_bt_run(["devices"]))
    conn_macs  = {d["mac"] for d in _bt_parse_device_list(_bt_run(["devices", "Connected"]))}
    paired_macs= {d["mac"] for d in _bt_parse_device_list(_bt_run(["devices", "Paired"]))}

    result = []
    for d in all_devs:
        result.append({
            "mac"      : d["mac"],
            "name"     : d["name"],
            "type"     : _bt_classify(d["name"]),
            "connected": d["mac"] in conn_macs,
            "paired"   : d["mac"] in paired_macs,
        })
    # Sort: connected → paired → alphabetical
    result.sort(key=lambda x: (
        0 if x["connected"] else (1 if x["paired"] else 2),
        x["name"].lower()
    ))

    conn_mac = next((d["mac"] for d in result if d["connected"]), None)
    with _bt_lock:
        bt_devices       = result
        bt_connected_mac = conn_mac

def _bt_scan_worker() -> None:
    global bt_scanning
    # Power is managed exclusively by bt_set_power — never touch it here.
    # Enable both BR/EDR (Classic — phones, headphones) and LE transports.
    # Without this, some adapters default to LE-only and miss phones entirely.
    _bt_run(["scan.transport", "auto"], timeout=3)

    # Start discovery via Popen so we control timing ourselves.
    # We write "scan on" then sleep while the process runs — do NOT use
    # communicate() here because that closes stdin and exits bluetoothctl
    # immediately, giving zero time to discover devices.
    proc = None
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(b"scan on\n")
        proc.stdin.flush()

        # Let it run for 15 s — phones need longer than speakers to respond.
        # Poll bt_refresh_devices midway so the UI shows devices as they appear.
        time.sleep(8)
        bt_refresh_devices()   # mid-scan refresh — shows partial results
        time.sleep(7)

    except Exception as e:
        print(f"BT scan error: {e}")
    finally:
        if proc:
            try:
                proc.stdin.write(b"scan off\nexit\n")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    bt_refresh_devices()
    with _bt_lock:
        bt_scanning = False
    print("🔵 BT scan complete")

def bt_start_scan() -> None:
    global bt_scanning, _bt_scan_thread
    with _bt_lock:
        if bt_scanning:
            return
        bt_scanning = True
    _bt_scan_thread = threading.Thread(target=_bt_scan_worker, daemon=True)
    _bt_scan_thread.start()
    print("🔵 BT scan started")

def bt_set_power(on: bool) -> None:
    def _worker():
        print(f"BT: requesting {'ON' if on else 'OFF'}...")
        try:
            if on:
                # Unblock rfkill — Pi Zero 2W ships with BT soft-blocked.
                # Safe to call even when already unblocked.
                subprocess.run(["sudo", "rfkill", "unblock", "bluetooth"],
                               capture_output=True, check=False)
                # Only restart the service if adapter is still blocked after
                # unblocking — avoids the 1.5 s penalty on every toggle.
                rfkill_out = subprocess.run(
                    ["rfkill", "list", "bluetooth"],
                    capture_output=True, text=True
                ).stdout
                if "blocked" in rfkill_out.lower():
                    print("BT: adapter still blocked — restarting service")
                    subprocess.run(["sudo", "systemctl", "restart", "bluetooth"],
                                   capture_output=True, check=False)
                    time.sleep(1.5)

            _bt_run(["power", "on" if on else "off"], timeout=8)

            if on:
                # FIX: Register agent unconditionally every time BT powers on.
                # Previously this only ran when rfkill was blocked, so if the
                # adapter was already powered the agent was never registered,
                # causing all pairing attempts to silently fail.
                time.sleep(0.5)   # give adapter a moment after power on
                _bt_run(["agent", "on"],   timeout=3)
                _bt_run(["default-agent"], timeout=3)
                print("BT: pairing agent registered")

        except Exception as e:
            print(f"bt_set_power setup error: {e}")

        # Poll until adapter state matches what was requested (max ~4 s).
        for _ in range(8):
            time.sleep(0.5)
            bt_refresh_devices()
            with _bt_lock:
                _current = bt_power
            if _current == on:
                print(f"BT: confirmed {'ON' if on else 'OFF'}")
                return
        print("BT: state did not confirm in time")

    threading.Thread(target=_worker, daemon=True).start()

def _pa_set_bt_sink(mac: str) -> bool:
    """
    Switch PulseAudio default sink to the A2DP profile for the given MAC.
    Returns True if the sink was set successfully.
    PulseAudio + pulseaudio-module-bluetooth must be installed for BT audio.
    """
    sink_name = "bluez_sink." + mac.replace(":", "_") + ".a2dp_sink"
    card_name  = "bluez_card." + mac.replace(":", "_")

    # Give PulseAudio a moment to register the new BT card before switching
    time.sleep(1.5)

    # Force A2DP (stereo audio) profile — without this it may stay on HFP/HSP
    # (mono phone-call quality). Retry a few times as the card may not be
    # registered in PulseAudio immediately after bluetoothctl reports connected.
    for attempt in range(6):
        r = subprocess.run(
            ["pactl", "set-card-profile", card_name, "a2dp-sink"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            break
        # Also try legacy profile name used by older bluez/PA versions
        r2 = subprocess.run(
            ["pactl", "set-card-profile", card_name, "a2dp_sink"],
            capture_output=True, text=True, timeout=5
        )
        if r2.returncode == 0:
            break
        print(f"  PA: card not ready yet (attempt {attempt+1}/6), waiting...")
        time.sleep(1.5)

    time.sleep(0.5)   # brief settle after profile switch

    try:
        subprocess.run(
            ["pactl", "set-default-sink", sink_name],
            capture_output=True, check=True, timeout=5
        )
        print(f"🔊 PulseAudio → {sink_name}")
        # Tell VLC to reopen its audio output on the new default sink
        _vlc_restart_audio()
        return True
    except subprocess.CalledProcessError:
        # sink_name with a2dp-sink suffix might differ — list and find it
        try:
            listed = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True, text=True, timeout=5
            ).stdout
            for line in listed.splitlines():
                parts = line.split()
                name = parts[1] if len(parts) > 1 else ""
                if "bluez" in name and mac.replace(":", "_") in name:
                    subprocess.run(
                        ["pactl", "set-default-sink", name],
                        capture_output=True, check=False, timeout=5
                    )
                    print(f"🔊 PulseAudio → {name} (auto-detected)")
                    _vlc_restart_audio()
                    return True
        except Exception:
            pass
        print(f"⚠ Could not set BT sink — is pulseaudio-module-bluetooth installed?")
        print(f"  Run: sudo apt install pulseaudio pulseaudio-module-bluetooth")
        return False
    except Exception as e:
        print(f"⚠ _pa_set_bt_sink error: {e}")
        return False

def _pa_reset_sink() -> None:
    """Restore PulseAudio default sink to the first non-Bluetooth output.
    Also tells VLC to reopen audio on the USB DAC."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            name = parts[1] if len(parts) > 1 else ""
            if name and "bluez" not in name:
                subprocess.run(
                    ["pactl", "set-default-sink", name],
                    capture_output=True, check=False, timeout=5
                )
                print(f"🔊 PulseAudio reset → {name}")
                _vlc_restart_audio()
                return
    except FileNotFoundError:
        pass   # pactl not installed — no PulseAudio, skip silently
    except Exception as e:
        print(f"Could not reset PulseAudio sink: {e}")

def bt_connect_device(mac: str) -> None:
    """
    Pair, trust, and connect a device interactively.

    Runs bluetoothctl in a persistent Popen session so we can watch its
    output line-by-line.  When the device asks for confirmation (SSP
    Just-Works "Confirm passkey" or legacy "Request confirmation"), we
    set bt_confirm_mac/pin and block until the user taps YES or NO on
    the on-screen overlay.  On YES we send "yes\n"; on NO we send "no\n"
    and abort.

    FIX 1: org.bluez.Error.AlreadyExists is treated as success (already
            paired), so we skip straight to trust+connect instead of
            looping until the 30-second deadline and then failing.
    FIX 2: A 2-second delay is added after first-time pairing success
            before calling connect, giving the headphones time to finish
            bonding before accepting the connection.
    FIX 3: pairing_ok flag is set for AlreadyExists so the trust+connect
            block runs rather than being silently skipped.
    """
    def _worker():
        global bt_confirm_mac, bt_confirm_name, bt_confirm_pin
        global _bt_confirm_answer
        print(f"🔵 Pairing/connecting {mac}")

        # Resolve a human-readable name from the current device list
        with _bt_lock:
            _name = next((d["name"] for d in bt_devices if d["mac"] == mac), mac)

        proc = None
        connected    = False
        pairing_ok   = False   # True once pair phase succeeds or was already done
        first_pair   = False   # True only when we actually paired (not AlreadyExists)
        abort        = False   # True when user rejects or an unrecoverable error hits
        try:
            proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            def _send(cmd: str) -> None:
                try:
                    proc.stdin.write(cmd + "\n")
                    proc.stdin.flush()
                except Exception:
                    pass

            # Wait briefly for bluetoothctl to initialise before sending pair
            time.sleep(0.3)
            _send(f"pair {mac}")

            deadline = time.monotonic() + 30   # max 30 s for pairing phase
            # Phase 2: after "Pairing successful", keep reading for up to 3 s
            # to catch the final Connected: yes/no before we close the session.
            # This tells us whether we need an explicit connect or not.
            settling_deadline = None

            import select as _select
            while time.monotonic() < deadline:
                # During the settling window, use select() with a short timeout
                # so readline() never blocks past the settling deadline.
                if settling_deadline is not None:
                    remaining = settling_deadline - time.monotonic()
                    if remaining <= 0:
                        break  # settling window expired — exit and connect
                    ready, _, _ = _select.select([proc.stdout], [], [], min(remaining, 0.5))
                    if not ready:
                        continue  # no data yet — loop back to check deadline
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                print(f"[BT] {line}")

                low = line.lower()

                # ── Device requests passkey / pin confirmation ───────
                if "confirm passkey" in low or "request confirmation" in low or "passkey" in low:
                    pin = ""
                    for token in line.split():
                        if token.isdigit() and len(token) >= 4:
                            pin = token
                            break
                    bt_confirm_mac  = mac
                    bt_confirm_name = _name
                    bt_confirm_pin  = pin
                    _bt_confirm_event.clear()
                    print(f"🔵 Pairing confirmation requested for {_name} (PIN: {pin!r})")

                    answered = _bt_confirm_event.wait(timeout=30)
                    bt_confirm_mac = None

                    if answered and _bt_confirm_answer:
                        print("🔵 User confirmed pairing")
                        _send("yes")
                    else:
                        print("🔵 User rejected / timed-out pairing")
                        _send("no")
                        abort = True
                        break

                # ── Already paired — treat as success ────────────────
                # MUST be before the generic "failed to pair" check because
                # bluetoothctl emits: "Failed to pair: org.bluez.Error.AlreadyExists"
                # which matches BOTH patterns.  AlreadyExists is never an error.
                elif "alreadyexists" in low.replace(".", "").replace(":", "").replace(" ", "") \
                     or "already paired" in low:
                    print("🔵 Already paired — skipping to connect")
                    pairing_ok = True
                    first_pair = False
                    break

                # ── Pairing succeeded — enter settling window ────────
                elif "pairing successful" in low:
                    print("🔵 Pairing successful")
                    pairing_ok = True
                    first_pair = True
                    # Read for up to 3 more seconds to see the final
                    # Connected: yes/no before closing the session.
                    settling_deadline = time.monotonic() + 3.0

                # ── Track connected state (only meaningful post-pairing) ──
                # Some headphones connect during pairing then immediately drop.
                # We track the LAST Connected line we see, so if the device
                # ends up on Connected: no we know to run an explicit connect.
                elif settling_deadline is not None:
                    if "connected: yes" in low:
                        connected = True
                    elif "connected: no" in low:
                        connected = False  # dropped — will need explicit connect

                # ── Pairing failed for a real reason ─────────────────
                elif "failed to pair" in low or ("auth" in low and "failed" in low):
                    print(f"⚠ Pairing failed: {line}")
                    abort = True
                    break

        except Exception as e:
            print(f"⚠ bt_connect_device error: {e}")
            abort = True
        finally:
            if proc:
                try:
                    proc.stdin.write("exit\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            # Always clear the confirmation overlay
            bt_confirm_mac = None

        if abort or not pairing_ok:
            bt_refresh_devices()
            return

        # ── Trust always (enables auto-reconnect in future) ────
        _bt_run(["trust", mac], timeout=5)

        if connected:
            # Device connected itself during the pairing handshake (common
            # with headphones). No need to issue a separate connect command.
            print("🔵 Already connected via pairing handshake")
        else:
            # Give headphones time to finish bonding after a fresh pair.
            # Not needed for AlreadyExists — device is already ready.
            if first_pair:
                print("🔵 Waiting for device to finish bonding…")
                time.sleep(5)   # A2DP profile needs ~4-5s to become available

            # ── Explicit connect via streaming Popen ──────────────
            # _bt_run() uses subprocess.run which exits before bluetoothctl
            # has streamed the connection result — so it always returns empty.
            # We must use Popen + readline() to catch the async result lines.
            def _bt_connect_blocking(mac: str, timeout_s: int = 15) -> bool:
                import select as _sel
                try:
                    cp = subprocess.Popen(
                        ["bluetoothctl"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True, bufsize=1,
                    )
                    cp.stdin.write(f"connect {mac}\n")
                    cp.stdin.flush()
                    deadline = time.monotonic() + timeout_s
                    result = False
                    while time.monotonic() < deadline:
                        remaining = deadline - time.monotonic()
                        ready, _, _ = _sel.select([cp.stdout], [], [], min(remaining, 0.5))
                        if not ready:
                            continue
                        line = cp.stdout.readline()
                        if not line:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        print(f"[BT-conn] {line}")
                        low = line.lower()
                        if "successful" in low or "already connected" in low or "connected: yes" in low:
                            result = True
                            break
                        # profile-unavailable means A2DP not ready yet — don't
                        # abort immediately, let the outer retry loop handle it
                        if "profile-unavailable" in low:
                            break
                        if "not available" in low or (
                                "failed" in low and "profile" not in low) or ("error" in low and "profile" not in low):
                            break
                    try:
                        cp.stdin.write("exit\n"); cp.stdin.flush()
                    except Exception:
                        pass
                    try:
                        cp.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cp.kill(); cp.communicate()
                    return result
                except Exception as e:
                    print(f"⚠ _bt_connect_blocking error: {e}")
                    return False

            for attempt in range(1, 6):
                print(f"🔵 Connect attempt {attempt}…")
                if _bt_connect_blocking(mac, timeout_s=15):
                    connected = True
                    print(f"🔵 Connected on attempt {attempt}")
                    break
                print(f"🔵 Connect attempt {attempt} failed — retrying...")
                time.sleep(3)   # give A2DP profile more time between attempts

        if connected:
            time.sleep(1)
            _pa_set_bt_sink(mac)
        else:
            print(f"⚠ Could not connect to {mac} after 3 attempts")

        bt_refresh_devices()

    threading.Thread(target=_worker, daemon=True).start()

def bt_disconnect_device(mac: str) -> None:
    def _worker():
        print(f"🔵 Disconnecting {mac}")
        _bt_run(["disconnect", mac], timeout=10)
        time.sleep(0.8)
        _pa_reset_sink()          # <-- Improved
        bt_refresh_devices()
    threading.Thread(target=_worker, daemon=True).start()

def bt_remove_device(mac: str) -> None:
    """Disconnect, untrust, and unpair a device so it no longer appears in Saved."""
    def _worker():
        print(f"🔵 Removing device {mac}")
        # Disconnect first if connected
        _bt_run(["disconnect", mac], timeout=10)
        time.sleep(0.3)
        # Remove (unpair + forget) from bluetoothctl
        out = _bt_run(["remove", mac], timeout=10)
        if "removed" in out.lower() or "not available" in out.lower():
            print(f"🔵 Device {mac} removed")
        else:
            print(f"⚠ Remove may have failed for {mac}: {out!r}")
        _pa_reset_sink()
        bt_refresh_devices()
    threading.Thread(target=_worker, daemon=True).start()

# ── BT icon colour (reflects live state, supports scan pulse) ─
def _bt_icon_color() -> tuple:
    with _bt_lock:
        _on        = bt_power
        _scanning  = bt_scanning
        _connected = bt_connected_mac is not None
    if not _on:
        return (130, 130, 150)
    if _connected:
        return (70, 210, 110)
    if _scanning:
        pulse = (math.sin(time.monotonic() * 5) + 1) / 2
        return (int(60 + pulse * 80), int(130 + pulse * 60), 255)
    return (80, 160, 255)


def draw_bt_confirm_overlay() -> None:
    """
    Modal overlay shown when a device requests pairing confirmation.
    Drawn on top of the BT screen.  Registers YES / NO hit-rects into
    bt_screen_rects so handle_tap() can respond without extra globals.
    """
    mac  = bt_confirm_mac
    name = bt_confirm_name
    pin  = bt_confirm_pin
    if not mac:
        return

    # ── Semi-transparent dim layer ────────────────────────────
    dim = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 180))
    screen.blit(dim, (0, 0))

    # ── Dialog box ────────────────────────────────────────────
    DLG_W, DLG_H = 290, 200
    dlg_x = (SCREEN_W - DLG_W) // 2
    dlg_y = (SCREEN_H - DLG_H) // 2
    dlg_r = pygame.Rect(dlg_x, dlg_y, DLG_W, DLG_H)

    pygame.draw.rect(screen, (28, 14, 68), dlg_r, border_radius=14)
    pygame.draw.rect(screen, (120, 90, 220), dlg_r, 2, border_radius=14)

    # ── Bluetooth icon at top of dialog ───────────────────────
    _draw_bt_symbol(screen, dlg_x + DLG_W // 2, dlg_y + 22, 10, (120, 160, 255), 2)

    # ── Title ─────────────────────────────────────────────────
    t1 = font_search.render("Pair with device?", True, TEXT_COLOR)
    screen.blit(t1, (dlg_x + DLG_W // 2 - t1.get_width() // 2, dlg_y + 38))

    # ── Device name ───────────────────────────────────────────
    dname = name[:26] + "…" if len(name) > 27 else name
    t2 = font_title.render(dname, True, (200, 220, 255))
    screen.blit(t2, (dlg_x + DLG_W // 2 - t2.get_width() // 2, dlg_y + 62))

    # ── Passkey (if present) ──────────────────────────────────
    if pin:
        lbl = font_small.render("Passkey:", True, TEXT_DIM)
        screen.blit(lbl, (dlg_x + DLG_W // 2 - lbl.get_width() // 2, dlg_y + 90))
        pk  = font_ctrl.render(pin, True, (255, 230, 80))
        screen.blit(pk,  (dlg_x + DLG_W // 2 - pk.get_width()  // 2, dlg_y + 106))
    else:
        hint = font_small.render("Confirm pairing on both devices", True, TEXT_DIM)
        screen.blit(hint, (dlg_x + DLG_W // 2 - hint.get_width() // 2, dlg_y + 94))

    # ── YES / NO buttons ──────────────────────────────────────
    btn_y   = dlg_y + DLG_H - 48
    btn_h   = 36
    btn_w   = 110
    gap     = 16
    total_w = btn_w * 2 + gap
    bx      = dlg_x + (DLG_W - total_w) // 2

    # NO  (left, red)
    no_r = pygame.Rect(bx, btn_y, btn_w, btn_h)
    pygame.draw.rect(screen, (160, 40, 40), no_r, border_radius=10)
    pygame.draw.rect(screen, (210, 80, 80), no_r, 2, border_radius=10)
    ns = font_search.render("Cancel", True, TEXT_COLOR)
    screen.blit(ns, (no_r.centerx - ns.get_width() // 2,
                     no_r.centery - ns.get_height() // 2))
    bt_screen_rects["BT_CONFIRM_NO"] = no_r

    # YES (right, green)
    yes_r = pygame.Rect(bx + btn_w + gap, btn_y, btn_w, btn_h)
    pygame.draw.rect(screen, (34, 148, 84), yes_r, border_radius=10)
    pygame.draw.rect(screen, (60, 200, 120), yes_r, 2, border_radius=10)
    ys = font_search.render("Confirm", True, TEXT_COLOR)
    screen.blit(ys, (yes_r.centerx - ys.get_width() // 2,
                     yes_r.centery - ys.get_height() // 2))
    bt_screen_rects["BT_CONFIRM_YES"] = yes_r


def draw_gradient_bg() -> None:
    screen.blit(_gradient_surf, (0, 0))

def _draw_key(rect: pygame.Rect,
              label: str,
              bg: tuple,
              highlight: tuple,
              font: pygame.font.Font) -> None:
    """Draw a single keyboard key with a top-edge highlight strip."""
    radius = 7
    # shadow (1 px down-right)
    shadow_r = rect.move(1, 1)
    pygame.draw.rect(screen, KB_SHADOW, shadow_r, border_radius=radius)
    # main body
    pygame.draw.rect(screen, bg, rect, border_radius=radius)
    # top highlight strip — gives a subtle 3-D raised look
    hi = pygame.Rect(rect.x + 2, rect.y + 1, rect.width - 4, 5)
    pygame.draw.rect(screen, highlight, hi, border_radius=3)
    # label
    surf = font.render(label, True, TEXT_COLOR)
    screen.blit(surf, (rect.centerx - surf.get_width() // 2,
                       rect.centery - surf.get_height() // 2 + 1))


# ══════════════════════════════════════════════════════════════
# BLUETOOTH DRAWING HELPERS
# ══════════════════════════════════════════════════════════════
def _draw_bt_symbol(surface: pygame.Surface,
                    cx: int, cy: int, r: int,
                    color: tuple, width: int = 2) -> None:
    """Draw the Bluetooth rune symbol centred at (cx, cy) with half-height r."""
    top = (cx,              cy - r)
    bot = (cx,              cy + r)
    mid = (cx,              cy)
    ur  = (cx + int(r * 0.6), cy - int(r * 0.46))
    lr  = (cx + int(r * 0.6), cy + int(r * 0.46))
    pygame.draw.line(surface, color, top, bot,  width)
    pygame.draw.line(surface, color, top, ur,   width)
    pygame.draw.line(surface, color, ur,  mid,  width)
    pygame.draw.line(surface, color, mid, lr,   width)
    pygame.draw.line(surface, color, lr,  bot,  width)


def _draw_device_icon(surface: pygame.Surface,
                      cx: int, cy: int, dev_type: str,
                      color: tuple = (210, 210, 255)) -> None:
    """Draw a small device-class icon centred at (cx, cy)."""
    if dev_type == "headphones":
        r = 11
        pygame.draw.arc(surface, color,
                        (cx - r, cy - r, r * 2, r * 2), 0, math.pi, 3)
        pygame.draw.rect(surface, color, (cx - r - 3, cy - 3,  7, 11), border_radius=3)
        pygame.draw.rect(surface, color, (cx + r - 4, cy - 3,  7, 11), border_radius=3)
    elif dev_type == "speaker":
        # Rounded body
        pygame.draw.rect(surface, color, (cx - 8, cy - 13, 16, 26), border_radius=4, width=2)
        # Woofer cone
        pygame.draw.circle(surface, color, (cx, cy - 1), 5, 2)
        pygame.draw.circle(surface, color, (cx, cy - 1), 2)
        # Port / tweeter
        pygame.draw.rect(surface, color, (cx - 4, cy + 8, 8, 3), border_radius=1)
    elif dev_type == "phone":
        pygame.draw.rect(surface, color, (cx - 8, cy - 14, 16, 28), border_radius=4, width=2)
        # Speaker grille
        pygame.draw.rect(surface, color, (cx - 3, cy - 12,  6,  2), border_radius=1)
        # Home button dot
        pygame.draw.circle(surface, color, (cx, cy + 10), 2)
    elif dev_type == "keyboard":
        pygame.draw.rect(surface, color, (cx - 14, cy - 5, 28, 12), border_radius=3, width=2)
        for i in range(4):
            pygame.draw.rect(surface, color, (cx - 9 + i * 6, cy - 2, 4, 4), border_radius=1)
    elif dev_type == "mouse":
        pygame.draw.ellipse(surface, color, (cx - 7, cy - 12, 14, 24), width=2)
        pygame.draw.line(surface, color, (cx, cy - 12), (cx, cy - 2), 2)
        pygame.draw.line(surface, color, (cx - 7, cy - 4), (cx + 7, cy - 4), 1)
    else:
        # Generic: circle with mini BT rune
        pygame.draw.circle(surface, color, (cx, cy), 13, 2)
        _draw_bt_symbol(surface, cx, cy, 6, color, 1)


_BT_ROWS_VISIBLE = 5   # max device rows shown without scrolling
_BT_ROW_H        = 56  # height of each device row
_BT_LIST_TOP     = 130 # y where device list begins (below tabs)

def draw_bluetooth_screen() -> None:
    """Full-screen Bluetooth panel with Available / Saved tabs."""
    global bt_screen_rects, bt_tab
    _new_rects = {}  # build into temp dict, atomically replace at end of draw

    with _bt_lock:
        _powered   = bt_power
        _scanning  = bt_scanning
        _devs_all  = list(bt_devices)
        _conn_mac  = bt_connected_mac

    # Split devices into the two tab lists
    # Available = devices seen during scan that are NOT yet paired
    # Saved     = devices that have been paired (registered) with this adapter
    _avail_devs = [d for d in _devs_all if not d["paired"]]
    _saved_devs = [d for d in _devs_all if d["paired"]]

    _scroll      = bt_scroll
    _saved_scroll = bt_saved_scroll

    # ── Header bar ───────────────────────────────────────────
    pygame.draw.rect(screen, (30, 15, 70),
                     pygame.Rect(0, 0, SCREEN_W, 50))

    _icon_col = _bt_icon_color()
    _draw_bt_symbol(screen, 24, 25, 13, _icon_col, 3)

    title_surf = font_title.render("Bluetooth", True, TEXT_COLOR)
    screen.blit(title_surf, (44, 15))

    # Power toggle button
    pow_label = "Turn OFF" if _powered else "Turn ON"
    pow_col   = (50, 200, 80) if _powered else (160, 60, 60)
    pow_r     = pygame.Rect(SCREEN_W - 36 - 4 - 84, 11, 84, 28)
    pygame.draw.rect(screen, pow_col, pow_r, border_radius=8)
    pygame.draw.rect(screen, (255, 255, 255), pow_r, 1, border_radius=8)
    ps = font_small.render(pow_label, True, TEXT_COLOR)
    screen.blit(ps, (pow_r.centerx - ps.get_width() // 2,
                     pow_r.centery - ps.get_height() // 2))
    _new_rects["POWER"] = pow_r

    # Close button
    close_r = pygame.Rect(SCREEN_W - 36, 11, 28, 28)
    pygame.draw.rect(screen, (180, 45, 45), close_r, border_radius=8)
    cs = font_key.render("X", True, TEXT_COLOR)
    screen.blit(cs, (close_r.centerx - cs.get_width() // 2,
                     close_r.centery - cs.get_height() // 2))
    _new_rects["CLOSE"] = close_r

    # ── Status bar ───────────────────────────────────────────
    pygame.draw.rect(screen, (20, 10, 55),
                     pygame.Rect(0, 50, SCREEN_W, 26))
    if not _powered:
        st_msg = "Bluetooth is off"
        st_col = (160, 160, 170)
    elif _scanning:
        dots   = "." * (int(time.monotonic() * 2) % 4)
        n_seen = len(_avail_devs)
        st_msg = f"Scanning{dots}  {n_seen} found" if n_seen else f"Scanning{dots}  ~15s"
        st_col = (120, 180, 255)
    elif _conn_mac:
        conn_name = next((d["name"] for d in _devs_all if d["mac"] == _conn_mac), _conn_mac)
        st_msg = f"Connected: {conn_name[:22]}"
        st_col = (70, 210, 110)
    else:
        st_msg = f"{len(_avail_devs)} nearby  ·  {len(_saved_devs)} saved"
        st_col = (200, 200, 210)
    screen.blit(font_small.render(st_msg, True, st_col), (12, 56))

    # ── Tab bar ───────────────────────────────────────────────
    TAB_Y  = 76
    TAB_H  = 30
    tab_w  = SCREEN_W // 2
    for tab_id, tab_label in (("available", "Available"), ("saved", "Saved")):
        tx    = 0 if tab_id == "available" else tab_w
        t_r   = pygame.Rect(tx, TAB_Y, tab_w, TAB_H)
        active = (bt_tab == tab_id)
        bg    = (60, 35, 130) if active else (25, 12, 60)
        pygame.draw.rect(screen, bg, t_r)
        # active tab gets a bright bottom underline
        if active:
            pygame.draw.line(screen, (140, 100, 255),
                             (tx, TAB_Y + TAB_H - 2),
                             (tx + tab_w, TAB_Y + TAB_H - 2), 2)
        count = len(_avail_devs) if tab_id == "available" else len(_saved_devs)
        lbl   = f"{tab_label} ({count})"
        ls    = font_small.render(lbl, True, TEXT_COLOR if active else TEXT_DIM)
        screen.blit(ls, (t_r.centerx - ls.get_width() // 2,
                         t_r.centery - ls.get_height() // 2))
        _new_rects[f"TAB_{tab_id.upper()}"] = t_r

    pygame.draw.line(screen, (80, 60, 130), (0, TAB_Y + TAB_H), (SCREEN_W, TAB_Y + TAB_H), 1)

    # ── Device list (tab-specific) ────────────────────────────
    if bt_tab == "available":
        _draw_bt_available_list(_avail_devs, _scroll, _new_rects)
        # Scan button at bottom
        _draw_bt_scan_button(_powered, _scanning, _new_rects)
    else:
        _draw_bt_saved_list(_saved_devs, _saved_scroll, _new_rects)

    # Atomically publish the new hit-rects so handle_tap never sees a half-built dict
    bt_screen_rects = _new_rects

    # ── Pairing confirmation overlay (drawn last so it's on top) ─
    if bt_confirm_mac:
        draw_bt_confirm_overlay()


def _draw_bt_available_list(devs: list, scroll: int, _new_rects: dict) -> None:
    """Draw the Available tab — nearby unpaired devices with a CONNECT button."""
    visible = devs[scroll : scroll + _BT_ROWS_VISIBLE]

    for idx, dev in enumerate(visible):
        y     = _BT_LIST_TOP + idx * _BT_ROW_H
        row_r = pygame.Rect(0, y, SCREEN_W, _BT_ROW_H - 2)

        row_bg = (45, 25, 90) if idx % 2 == 0 else (35, 18, 75)
        pygame.draw.rect(screen, row_bg, row_r)

        _draw_device_icon(screen, 22, y + _BT_ROW_H // 2, dev["type"], (200, 200, 255))

        name_str = dev["name"][:17] + "…" if len(dev["name"]) > 18 else dev["name"]
        screen.blit(font_search.render(name_str, True, TEXT_COLOR), (46, y + 8))
        screen.blit(font_small.render(dev["type"].capitalize(), True, TEXT_DIM), (46, y + 30))

        btn_r = pygame.Rect(SCREEN_W - 88, y + 14, 82, 26)
        pygame.draw.rect(screen, (40, 120, 80), btn_r, border_radius=6)
        pygame.draw.rect(screen, (60, 170, 110), btn_r, 2, border_radius=6)
        bls = font_small.render("CONNECT", True, TEXT_COLOR)
        screen.blit(bls, (btn_r.centerx - bls.get_width() // 2,
                          btn_r.centery - bls.get_height() // 2))
        _new_rects[f"AVAIL_{scroll + idx}"] = btn_r

        pygame.draw.line(screen, (60, 40, 100),
                         (0, y + _BT_ROW_H - 2), (SCREEN_W, y + _BT_ROW_H - 2), 1)

    if not devs:
        msg  = font_search.render("No nearby devices", True, TEXT_DIM)
        hint = font_small.render("Tap SCAN to search", True, TEXT_DIM)
        screen.blit(msg,  (SCREEN_W // 2 - msg.get_width()  // 2, 210))
        screen.blit(hint, (SCREEN_W // 2 - hint.get_width() // 2, 236))

    # Scroll arrows
    list_bottom = _BT_LIST_TOP + _BT_ROWS_VISIBLE * _BT_ROW_H
    if scroll > 0:
        up_r = pygame.Rect(SCREEN_W // 2 - 20, _BT_LIST_TOP - 18, 40, 16)
        pygame.draw.rect(screen, (80, 60, 140), up_r, border_radius=5)
        screen.blit(font_small.render("▲", True, TEXT_COLOR), (up_r.centerx - 6, up_r.y + 1))
        _new_rects["SCROLL_UP"] = up_r
    if scroll + _BT_ROWS_VISIBLE < len(devs):
        dn_r = pygame.Rect(SCREEN_W // 2 - 20, list_bottom + 2, 40, 16)
        pygame.draw.rect(screen, (80, 60, 140), dn_r, border_radius=5)
        screen.blit(font_small.render("▼", True, TEXT_COLOR), (dn_r.centerx - 6, dn_r.y + 1))
        _new_rects["SCROLL_DN"] = dn_r


def _draw_bt_saved_list(devs: list, scroll: int, _new_rects: dict) -> None:
    """Draw the Saved tab — paired devices with CONNECT/DISCONNECT + REMOVE (×)."""
    visible = devs[scroll : scroll + _BT_ROWS_VISIBLE]

    for idx, dev in enumerate(visible):
        y     = _BT_LIST_TOP + idx * _BT_ROW_H
        row_r = pygame.Rect(0, y, SCREEN_W, _BT_ROW_H - 2)

        row_bg = (20, 70, 40) if dev["connected"] else (
                 (45, 25, 90) if idx % 2 == 0 else (35, 18, 75))
        pygame.draw.rect(screen, row_bg, row_r)

        _draw_device_icon(screen, 22, y + _BT_ROW_H // 2, dev["type"], (200, 200, 255))

        name_str = dev["name"][:15] + "…" if len(dev["name"]) > 16 else dev["name"]
        screen.blit(font_search.render(name_str, True, TEXT_COLOR), (46, y + 8))

        type_lbl = dev["type"].capitalize() + (" · Connected" if dev["connected"] else " · Paired")
        screen.blit(font_small.render(type_lbl, True,
                    (100, 230, 130) if dev["connected"] else TEXT_DIM), (46, y + 30))

        # Remove (×) button — far right, small red square
        rem_r = pygame.Rect(SCREEN_W - 30, y + 16, 24, 24)
        pygame.draw.rect(screen, (140, 30, 30), rem_r, border_radius=5)
        xs = font_small.render("×", True, TEXT_COLOR)
        screen.blit(xs, (rem_r.centerx - xs.get_width() // 2,
                         rem_r.centery - xs.get_height() // 2))
        _new_rects[f"REM_{scroll + idx}"] = rem_r

        # Connect / Disconnect button — left of Remove
        if dev["connected"]:
            btn_label, btn_col, btn_lt = "DISCON.", (160, 40, 40), (200, 70, 70)
        else:
            btn_label, btn_col, btn_lt = "CONNECT", (40, 100, 190), (60, 140, 230)
        btn_r = pygame.Rect(SCREEN_W - 114, y + 16, 80, 24)
        pygame.draw.rect(screen, btn_col, btn_r, border_radius=6)
        pygame.draw.rect(screen, btn_lt,  btn_r, 2, border_radius=6)
        bls = font_small.render(btn_label, True, TEXT_COLOR)
        screen.blit(bls, (btn_r.centerx - bls.get_width() // 2,
                          btn_r.centery - bls.get_height() // 2))
        _new_rects[f"SAVED_{scroll + idx}"] = btn_r

        pygame.draw.line(screen, (60, 40, 100),
                         (0, y + _BT_ROW_H - 2), (SCREEN_W, y + _BT_ROW_H - 2), 1)

    if not devs:
        msg  = font_search.render("No saved devices", True, TEXT_DIM)
        hint = font_small.render("Pair a device from the Available tab", True, TEXT_DIM)
        screen.blit(msg,  (SCREEN_W // 2 - msg.get_width()  // 2, 210))
        screen.blit(hint, (SCREEN_W // 2 - hint.get_width() // 2, 236))

    # Scroll arrows
    list_bottom = _BT_LIST_TOP + _BT_ROWS_VISIBLE * _BT_ROW_H
    if scroll > 0:
        up_r = pygame.Rect(SCREEN_W // 2 - 20, _BT_LIST_TOP - 18, 40, 16)
        pygame.draw.rect(screen, (80, 60, 140), up_r, border_radius=5)
        screen.blit(font_small.render("▲", True, TEXT_COLOR), (up_r.centerx - 6, up_r.y + 1))
        _new_rects["SAVED_SCROLL_UP"] = up_r
    if scroll + _BT_ROWS_VISIBLE < len(devs):
        dn_r = pygame.Rect(SCREEN_W // 2 - 20, list_bottom + 2, 40, 16)
        pygame.draw.rect(screen, (80, 60, 140), dn_r, border_radius=5)
        screen.blit(font_small.render("▼", True, TEXT_COLOR), (dn_r.centerx - 6, dn_r.y + 1))
        _new_rects["SAVED_SCROLL_DN"] = dn_r


def _draw_bt_scan_button(_powered: bool, _scanning: bool, _new_rects: dict) -> None:
    """Draw the SCAN button at the bottom of the Available tab."""
    scan_y = SCREEN_H - 60
    if not _powered:
        return
    if _scanning:
        scan_label, scan_col, scan_lt = "Scanning…", (60, 80, 160), (80, 110, 210)
    else:
        scan_label, scan_col, scan_lt = "SCAN FOR DEVICES", (60, 40, 130), (90, 65, 185)
    scan_r = pygame.Rect(16, scan_y, SCREEN_W - 32, 44)
    pygame.draw.rect(screen, scan_col, scan_r, border_radius=10)
    pygame.draw.rect(screen, scan_lt,  scan_r, 2, border_radius=10)
    _draw_bt_symbol(screen, scan_r.x + 24, scan_r.centery, 10, TEXT_COLOR, 2)
    ss = font_search.render(scan_label, True, TEXT_COLOR)
    screen.blit(ss, (scan_r.centerx - ss.get_width() // 2 + 10,
                     scan_r.centery - ss.get_height() // 2))
    if not _scanning:
        _new_rects["SCAN"] = scan_r



# ══════════════════════════════════════════════════════════════
# WIFI  (uses nmcli — no extra Python deps)
# ══════════════════════════════════════════════════════════════

def _wifi_run(args: list, timeout: int = 6) -> str:
    """Run an nmcli sub-command and return stdout."""
    try:
        r = subprocess.run(
            ["nmcli"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        print(f"nmcli {args}: {e}")
        return ""

def _wifi_active_iface() -> str:
    """Return the first WiFi interface name, or empty string if unknown."""
    out = _wifi_run(["--terse", "-f", "DEVICE,TYPE", "device"], timeout=6)
    for line in out.splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2 and parts[1].strip() == "wifi":
            return parts[0].strip()
    return ""

def _wifi_list_networks() -> str:
    """Return the cached list of visible WiFi networks from NetworkManager.
    Never triggers a rescan here — the scan worker handles that separately
    to avoid double-rescan stalls on the Pi Zero 2W wlan driver.
    """
    iface = _wifi_active_iface()
    # Use --rescan no: rely on already-scanned data; avoids double-rescan stalls.
    args = [
        "--terse",
        "-f", "SSID,SIGNAL,SECURITY,IN-USE",
        "device", "wifi", "list",
        "--rescan", "no",
    ]
    if iface:
        args += ["ifname", iface]
    out = _wifi_run(args, timeout=10)
    if out:
        return out
    # Fallback: no ifname, no rescan flag
    out = _wifi_run(
        ["--terse", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list"],
        timeout=10,
    )
    return out

def _nmcli_unescape(text: str) -> str:
    r"""Unescape nmcli --terse output (\: and \\)."""
    return text.replace("\\:", ":").replace("\\\\", "\\")

def _nmcli_terse_split(line: str, maxfields: int) -> list:
    """Split an nmcli --terse line on unescaped colons only.

    nmcli escapes literal colons inside values as \\: and backslashes as \\\\.
    A naive split(":", n) breaks on SSIDs/security strings that contain colons.
    This scanner consumes the line char-by-char and only splits on bare ':'.
    """
    fields = []
    current = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            # escaped character — keep both chars in the current field
            current.append(line[i + 1])
            i += 2
            continue
        if ch == ":" and len(fields) < maxfields - 1:
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    fields.append("".join(current))
    return fields

def wifi_refresh_networks() -> None:
    """Poll nmcli for current network state (safe to call from any thread)."""
    global wifi_networks, wifi_connected_ssid

    # Detect active WiFi interface (don't hardcode wlan0)
    iface = _wifi_active_iface() or "wlan0"

    # Currently connected SSID — query the specific interface
    conn_out  = _wifi_run(
        ["--terse", "-f", "GENERAL.CONNECTION", "device", "show", iface],
        timeout=6,
    )
    conn_ssid = None
    for line in conn_out.splitlines():
        if "GENERAL.CONNECTION:" in line:
            val = line.split(":", 1)[1].strip()
            if val and val not in ("--", ""):
                conn_ssid = val

    # Saved (configured) wireless connections — profile name = SSID in most cases
    saved_out  = _wifi_run(["--terse", "-f", "NAME,TYPE", "connection", "show"], timeout=6)
    saved_ssids: set = set()
    for line in saved_out.splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2 and "wireless" in parts[1].lower():
            saved_ssids.add(_nmcli_unescape(parts[0].strip()))

    # Network list — parsed with escape-aware splitter
    scan_out = _wifi_list_networks()

    seen: set = set()
    result: list = []
    for line in scan_out.splitlines():
        if not line.strip():
            continue
        parts = _nmcli_terse_split(line, 4)
        if len(parts) < 4:
            continue
        ssid_raw, signal_s, security_raw, in_use = parts
        ssid     = ssid_raw.strip()
        security = security_raw.strip()
        in_use   = in_use.strip()

        if not ssid:
            ssid = "<hidden>"
        if ssid in seen:
            continue
        seen.add(ssid)

        try:
            signal = int(signal_s.strip())
        except ValueError:
            signal = 0

        # security field may be "WPA2" / "WPA1 WPA2" / "--" / ""
        if not security or security == "--":
            security = "Open"

        result.append({
            "ssid"     : ssid,
            "signal"   : signal,
            "security" : security,
            "connected": in_use == "*",
            "saved"    : ssid in saved_ssids,
        })

    # Sort: connected first, then saved, then by signal strength descending
    result.sort(
        key=lambda x: (
            0 if x["connected"] else (1 if x["saved"] else 2),
            -x["signal"],
        )
    )

    with _wifi_lock:
        wifi_networks       = result
        wifi_connected_ssid = conn_ssid

def _wifi_scan_worker() -> None:
    global wifi_scanning
    try:
        iface = _wifi_active_iface() or "wlan0"
        # Trigger a driver-level rescan via nmcli
        rescan_args = ["device", "wifi", "rescan"]
        if iface:
            rescan_args += ["ifname", iface]
        subprocess.run(
            ["nmcli"] + rescan_args,
            capture_output=True, timeout=10
        )
        # Wait for the driver to gather results — Pi Zero 2W needs ~5 s
        time.sleep(5)
        wifi_refresh_networks()
        # A second pass a few seconds later picks up late-arriving beacons
        time.sleep(3)
        wifi_refresh_networks()
    except Exception as e:
        print(f"WiFi scan error: {e}")
    finally:
        with _wifi_lock:
            wifi_scanning = False
    print("📶 WiFi scan complete")

def wifi_start_scan() -> None:
    global wifi_scanning
    with _wifi_lock:
        if wifi_scanning:
            return
        wifi_scanning = True
    threading.Thread(target=_wifi_scan_worker, daemon=True).start()
    print("📶 WiFi scan started")

def wifi_connect(ssid: str, password: str = "") -> None:
    """Connect to a WiFi network (with or without password).

    Always uses `nmcli device wifi connect` which works for both new networks
    and saved profiles — NetworkManager automatically reuses stored credentials
    when no password is supplied and a profile exists.
    """
    def _worker():
        print(f"📶 Connecting to '{ssid}'…")
        args = ["device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        iface = _wifi_active_iface()
        if iface:
            args += ["ifname", iface]
        # Allow up to 30 s for WPA handshake + DHCP
        out = _wifi_run(args, timeout=35)
        if out and ("successfully" in out.lower() or "activated" in out.lower()):
            print(f"✅ WiFi connected to '{ssid}'")
        else:
            print(f"⚠ WiFi connect result for '{ssid}': {out!r}")
        time.sleep(1)
        wifi_refresh_networks()
    threading.Thread(target=_worker, daemon=True).start()

def wifi_disconnect() -> None:
    """Disconnect the current WiFi connection."""
    def _worker():
        print("📶 Disconnecting WiFi…")
        _wifi_run(["device", "disconnect", "wlan0"], timeout=8)
        time.sleep(0.5)
        wifi_refresh_networks()
    threading.Thread(target=_worker, daemon=True).start()

def wifi_forget(ssid: str) -> None:
    """Remove (forget) a saved WiFi connection."""
    def _worker():
        print(f"📶 Forgetting '{ssid}'…")
        out = _wifi_run(["connection", "delete", ssid], timeout=8)
        print(f"  → {out!r}")
        wifi_refresh_networks()
    threading.Thread(target=_worker, daemon=True).start()

# ── WiFi icon colour ──────────────────────────────────────────
def _wifi_icon_color() -> tuple:
    with _wifi_lock:
        _connected = wifi_connected_ssid is not None
        _scanning  = wifi_scanning
    if _connected:
        return (70, 210, 110)
    if _scanning:
        pulse = (math.sin(time.monotonic() * 5) + 1) / 2
        return (int(60 + pulse * 80), int(130 + pulse * 60), 255)
    return (80, 160, 255)

def _draw_wifi_symbol(surface: pygame.Surface,
                      cx: int, cy: int, r: int,
                      color: tuple, width: int = 2) -> None:
    """Draw a simple WiFi arc symbol centred at (cx, cy)."""
    # Three concentric arcs (outer → inner) + dot at bottom
    for i, radius in enumerate([r, int(r * 0.65), int(r * 0.32)]):
        if radius < 3:
            continue
        rect = pygame.Rect(cx - radius, cy - radius // 2, radius * 2, radius * 2)
        try:
            pygame.draw.arc(surface, color, rect,
                            math.radians(30), math.radians(150), max(1, width - i))
        except Exception:
            pass
    pygame.draw.circle(surface, color, (cx, cy + int(r * 0.15)), max(2, width))

# ── WiFi screen constants ─────────────────────────────────────
# 4 rows visible so the bottom SCAN button has clear space below the list.
# Layout math: header(50) + status(26) + tabs(30) + 4×row(52) + gap(4) = 318
# Bottom scan button sits at y=418 (SCREEN_H-62) with 44px height → fits 480px.
_WIFI_ROWS_VISIBLE = 4
_WIFI_ROW_H        = 52
_WIFI_LIST_TOP     = 110   # y where device rows begin (below tabs)


def _draw_signal_bars(surface: pygame.Surface,
                      bx: int, by: int, signal: int) -> None:
    """Draw 4 vertical signal-strength bars at (bx, by) bottom-left origin."""
    # Decide how many bars are "lit" (1-4) based on signal 0-100
    if signal >= 75:
        lit, color = 4, (70, 215, 115)
    elif signal >= 50:
        lit, color = 3, (180, 215, 75)
    elif signal >= 25:
        lit, color = 2, (215, 160, 45)
    else:
        lit, color = 1, (200, 75, 75)
    dim = (60, 60, 80)
    bar_w = 5
    gap   = 3
    heights = [6, 10, 14, 18]   # shortest → tallest
    for i, h in enumerate(heights):
        x = bx + i * (bar_w + gap)
        y = by - h
        col = color if i < lit else dim
        pygame.draw.rect(surface, col, (x, y, bar_w, h), border_radius=2)


def _draw_lock_icon(surface: pygame.Surface,
                    cx: int, cy: int, color: tuple) -> None:
    """Draw a small padlock icon centred at (cx, cy)."""
    # Body
    pygame.draw.rect(surface, color,
                     pygame.Rect(cx - 5, cy - 1, 10, 8), border_radius=2)
    # Shackle arc (top half of an ellipse)
    pygame.draw.arc(surface, color,
                    pygame.Rect(cx - 4, cy - 8, 8, 10),
                    0, math.pi, 2)


def draw_wifi_screen() -> None:
    """Full-screen WiFi panel with Available / Saved tabs and bottom SCAN button."""
    global wifi_screen_rects, wifi_tab
    _new_rects = {}

    with _wifi_lock:
        _scanning  = wifi_scanning
        _nets_all  = list(wifi_networks)
        _conn_ssid = wifi_connected_ssid

    _avail_nets = [n for n in _nets_all if not n["saved"]]
    _saved_nets = [n for n in _nets_all if n["saved"]]
    _scroll     = wifi_scroll
    _saved_scrl = wifi_saved_scroll

    # ── Header (50 px) ───────────────────────────────────────
    pygame.draw.rect(screen, (8, 24, 56),
                     pygame.Rect(0, 0, SCREEN_W, 50))
    # thin bottom accent line
    pygame.draw.line(screen, (30, 90, 180), (0, 49), (SCREEN_W, 49), 1)

    _icon_col = _wifi_icon_color()
    _draw_wifi_symbol(screen, 26, 25, 13, _icon_col, 2)

    title_surf = font_title.render("Wi-Fi", True, TEXT_COLOR)
    screen.blit(title_surf, (48, 15))

    # Connected SSID badge in header (if connected)
    if _conn_ssid:
        badge_s = font_small.render(_conn_ssid[:20], True, (80, 220, 130))
        bx = SCREEN_W // 2 - badge_s.get_width() // 2
        screen.blit(badge_s, (bx, 19))

    # Close button — top-right only
    close_r = pygame.Rect(SCREEN_W - 38, 11, 28, 28)
    pygame.draw.rect(screen, (170, 40, 40), close_r, border_radius=8)
    pygame.draw.rect(screen, (220, 80, 80), close_r, 1, border_radius=8)
    cs = font_key.render("X", True, TEXT_COLOR)
    screen.blit(cs, (close_r.centerx - cs.get_width() // 2,
                     close_r.centery - cs.get_height() // 2))
    _new_rects["WIFI_CLOSE"] = close_r

    # ── Status bar (26 px) ───────────────────────────────────
    pygame.draw.rect(screen, (5, 15, 40),
                     pygame.Rect(0, 50, SCREEN_W, 30))
    pygame.draw.line(screen, (20, 55, 110), (0, 79), (SCREEN_W, 79), 1)

    if _conn_ssid:
        st_msg = f"Connected: {_conn_ssid[:22]}"
        st_col = (70, 215, 115)
    elif _scanning:
        dots   = "." * (int(time.monotonic() * 2) % 4)
        n_seen = len(_avail_nets)
        st_msg = f"Scanning{dots}  {n_seen} found" if n_seen else f"Scanning{dots}"
        st_col = (100, 170, 255)
    else:
        st_msg = f"{len(_avail_nets)} nearby  ·  {len(_saved_nets)} saved"
        st_col = (180, 180, 210)
    screen.blit(font_small.render(st_msg, True, st_col), (12, 59))

    # ── Tab bar (30 px) ──────────────────────────────────────
    TAB_Y = 80
    TAB_H = 30
    tab_w = SCREEN_W // 2
    for tab_id, tab_label in (("available", "Available"), ("saved", "Saved")):
        tx     = 0 if tab_id == "available" else tab_w
        t_r    = pygame.Rect(tx, TAB_Y, tab_w, TAB_H)
        active = (wifi_tab == tab_id)
        bg     = (18, 50, 105) if active else (8, 20, 48)
        pygame.draw.rect(screen, bg, t_r)
        if active:
            pygame.draw.line(screen, (50, 130, 255),
                             (tx + 4, TAB_Y + TAB_H - 2),
                             (tx + tab_w - 4, TAB_Y + TAB_H - 2), 2)
        count = len(_avail_nets) if tab_id == "available" else len(_saved_nets)
        lbl   = f"{tab_label}  ({count})"
        ls    = font_small.render(lbl, True, TEXT_COLOR if active else TEXT_DIM)
        screen.blit(ls, (t_r.centerx - ls.get_width() // 2,
                         t_r.centery - ls.get_height() // 2))
        _new_rects[f"WIFI_TAB_{tab_id.upper()}"] = t_r

    # ── Network list ─────────────────────────────────────────
    if wifi_tab == "available":
        _draw_wifi_available_list(_avail_nets, _scroll, _new_rects, _conn_ssid)
        _draw_wifi_scan_button(_scanning, _new_rects)
    else:
        _draw_wifi_saved_list(_saved_nets, _saved_scrl, _new_rects, _conn_ssid)

    wifi_screen_rects = _new_rects

    # ── Password overlay (drawn last, on top) ─────────────────
    if wifi_pw_ssid:
        _draw_wifi_password_overlay()


def _draw_wifi_scan_button(_scanning: bool, _new_rects: dict) -> None:
    """Full-width SCAN FOR NETWORKS button at the bottom — mirrors BT scan button."""
    scan_y = SCREEN_H - 62
    if _scanning:
        scan_label = "Scanning…"
        scan_col   = (20, 55, 120)
        scan_lt    = (40, 90, 180)
    else:
        scan_label = "SCAN FOR NETWORKS"
        scan_col   = (14, 48, 110)
        scan_lt    = (35, 90, 200)
    scan_r = pygame.Rect(16, scan_y, SCREEN_W - 32, 44)
    pygame.draw.rect(screen, scan_col, scan_r, border_radius=10)
    pygame.draw.rect(screen, scan_lt,  scan_r, 2, border_radius=10)
    # WiFi icon left of label
    _draw_wifi_symbol(screen, scan_r.x + 24, scan_r.centery + 2, 10, TEXT_COLOR, 2)
    ss = font_search.render(scan_label, True, TEXT_COLOR)
    screen.blit(ss, (scan_r.centerx - ss.get_width() // 2 + 12,
                     scan_r.centery - ss.get_height() // 2))
    if not _scanning:
        _new_rects["WIFI_SCAN"] = scan_r


def _draw_wifi_available_list(nets: list, scroll: int,
                               _new_rects: dict, conn_ssid) -> None:
    visible = nets[scroll: scroll + _WIFI_ROWS_VISIBLE]

    for idx, net in enumerate(visible):
        y      = _WIFI_LIST_TOP + idx * _WIFI_ROW_H
        row_r  = pygame.Rect(0, y, SCREEN_W, _WIFI_ROW_H - 1)
        is_con = net["ssid"] == conn_ssid

        # Row background — alternate shading, connected gets a teal tint
        if is_con:
            row_bg = (8, 38, 24)
        elif idx % 2 == 0:
            row_bg = (12, 34, 72)
        else:
            row_bg = (8, 24, 55)
        pygame.draw.rect(screen, row_bg, row_r)

        # Left accent strip for connected row
        if is_con:
            pygame.draw.rect(screen, (50, 200, 100),
                             pygame.Rect(0, y, 3, _WIFI_ROW_H - 1))

        # Signal bars — bottom-aligned to row centre
        _draw_signal_bars(screen, 8, y + _WIFI_ROW_H - 10, net["signal"])

        # Lock icon or "Open" badge
        sec = net["security"]
        if sec != "Open":
            _draw_lock_icon(screen, 46, y + _WIFI_ROW_H // 2, (180, 180, 220))
            sec_x = 56
        else:
            sec_x = 10   # no lock
        _ = sec_x   # used below for label offset

        # SSID name
        ssid_s = net["ssid"][:17] + "…" if len(net["ssid"]) > 18 else net["ssid"]
        name_col = (80, 225, 130) if is_con else TEXT_COLOR
        screen.blit(font_search.render(ssid_s, True, name_col), (58, y + 7))

        # Sub-label: security type / "Connected"
        if is_con:
            sub_lbl = "Connected"
            sub_col = (80, 215, 130)
        else:
            sub_lbl = sec if sec != "Open" else "Open network"
            sub_col = TEXT_DIM
        screen.blit(font_small.render(sub_lbl, True, sub_col), (58, y + 30))

        # CONNECT button (right side)
        btn_r = pygame.Rect(SCREEN_W - 86, y + 13, 80, 26)
        b_col = (15, 70, 140) if not is_con else (20, 100, 55)
        b_lt  = (35, 110, 210) if not is_con else (40, 160, 90)
        b_lbl = "CONNECT" if not is_con else "CONNECTED"
        pygame.draw.rect(screen, b_col, btn_r, border_radius=6)
        pygame.draw.rect(screen, b_lt,  btn_r, 2, border_radius=6)
        bls = font_small.render(b_lbl, True, TEXT_COLOR)
        screen.blit(bls, (btn_r.centerx - bls.get_width() // 2,
                          btn_r.centery - bls.get_height() // 2))
        _new_rects[f"WIFI_AVAIL_{scroll + idx}"] = btn_r

        # Row divider
        pygame.draw.line(screen, (18, 45, 95),
                         (0, y + _WIFI_ROW_H - 1),
                         (SCREEN_W, y + _WIFI_ROW_H - 1), 1)

    if not nets:
        # Empty-state card
        card_r = pygame.Rect(20, _WIFI_LIST_TOP + 10, SCREEN_W - 40, 90)
        pygame.draw.rect(screen, (10, 28, 62), card_r, border_radius=12)
        pygame.draw.rect(screen, (30, 70, 140), card_r, 1, border_radius=12)
        _draw_wifi_symbol(screen, card_r.centerx, card_r.y + 28, 14,
                          (60, 100, 200), 2)
        msg  = font_search.render("No networks found", True, TEXT_DIM)
        hint = font_small.render("Tap SCAN FOR NETWORKS below", True, (100, 130, 200))
        screen.blit(msg,  (card_r.centerx - msg.get_width()  // 2, card_r.y + 50))
        screen.blit(hint, (card_r.centerx - hint.get_width() // 2, card_r.y + 72))

    # Scroll arrows
    list_bottom = _WIFI_LIST_TOP + _WIFI_ROWS_VISIBLE * _WIFI_ROW_H
    if scroll > 0:
        up_r = pygame.Rect(SCREEN_W // 2 - 22, _WIFI_LIST_TOP - 16, 44, 14)
        pygame.draw.rect(screen, (20, 55, 120), up_r, border_radius=4)
        us = font_small.render("  ▲", True, TEXT_COLOR)
        screen.blit(us, (up_r.x + 4, up_r.y + 1))
        _new_rects["WIFI_SCROLL_UP"] = up_r
    if scroll + _WIFI_ROWS_VISIBLE < len(nets):
        dn_r = pygame.Rect(SCREEN_W // 2 - 22, list_bottom + 2, 44, 14)
        pygame.draw.rect(screen, (20, 55, 120), dn_r, border_radius=4)
        ds = font_small.render("  ▼", True, TEXT_COLOR)
        screen.blit(ds, (dn_r.x + 4, dn_r.y + 1))
        _new_rects["WIFI_SCROLL_DN"] = dn_r


def _draw_wifi_saved_list(nets: list, scroll: int,
                           _new_rects: dict, conn_ssid) -> None:
    visible = nets[scroll: scroll + _WIFI_ROWS_VISIBLE]

    for idx, net in enumerate(visible):
        y      = _WIFI_LIST_TOP + idx * _WIFI_ROW_H
        row_r  = pygame.Rect(0, y, SCREEN_W, _WIFI_ROW_H - 1)
        is_con = net["ssid"] == conn_ssid

        if is_con:
            row_bg = (8, 38, 24)
        elif idx % 2 == 0:
            row_bg = (12, 34, 72)
        else:
            row_bg = (8, 24, 55)
        pygame.draw.rect(screen, row_bg, row_r)

        if is_con:
            pygame.draw.rect(screen, (50, 200, 100),
                             pygame.Rect(0, y, 3, _WIFI_ROW_H - 1))

        # Lock icon for secured networks
        sec = net["security"]
        if sec != "Open":
            _draw_lock_icon(screen, 20, y + _WIFI_ROW_H // 2, (180, 180, 220))

        ssid_s = net["ssid"][:16] + "…" if len(net["ssid"]) > 17 else net["ssid"]
        name_col = (80, 225, 130) if is_con else TEXT_COLOR
        screen.blit(font_search.render(ssid_s, True, name_col), (34, y + 7))

        if is_con:
            sub_lbl = "Connected"
            sub_col = (80, 215, 130)
        else:
            sub_lbl = "Saved  " + sec
            sub_col = TEXT_DIM
        screen.blit(font_small.render(sub_lbl, True, sub_col), (34, y + 30))

        # Remove (×) button
        rem_r = pygame.Rect(SCREEN_W - 32, y + 14, 26, 24)
        pygame.draw.rect(screen, (130, 28, 28), rem_r, border_radius=5)
        pygame.draw.rect(screen, (190, 60, 60), rem_r, 1, border_radius=5)
        xs = font_small.render("x", True, TEXT_COLOR)
        screen.blit(xs, (rem_r.centerx - xs.get_width() // 2,
                         rem_r.centery - xs.get_height() // 2))
        _new_rects[f"WIFI_REM_{scroll + idx}"] = rem_r

        # Connect / Disconnect button
        if is_con:
            btn_label, btn_col, btn_lt = "DISCON.", (120, 30, 30), (180, 60, 60)
        else:
            btn_label, btn_col, btn_lt = "CONNECT", (15, 68, 150), (35, 105, 220)
        btn_r = pygame.Rect(SCREEN_W - 118, y + 14, 82, 24)
        pygame.draw.rect(screen, btn_col, btn_r, border_radius=6)
        pygame.draw.rect(screen, btn_lt,  btn_r, 2, border_radius=6)
        bls = font_small.render(btn_label, True, TEXT_COLOR)
        screen.blit(bls, (btn_r.centerx - bls.get_width() // 2,
                          btn_r.centery - bls.get_height() // 2))
        _new_rects[f"WIFI_SAVED_{scroll + idx}"] = btn_r

        pygame.draw.line(screen, (18, 45, 95),
                         (0, y + _WIFI_ROW_H - 1),
                         (SCREEN_W, y + _WIFI_ROW_H - 1), 1)

    if not nets:
        card_r = pygame.Rect(20, _WIFI_LIST_TOP + 10, SCREEN_W - 40, 90)
        pygame.draw.rect(screen, (10, 28, 62), card_r, border_radius=12)
        pygame.draw.rect(screen, (30, 70, 140), card_r, 1, border_radius=12)
        msg  = font_search.render("No saved networks", True, TEXT_DIM)
        hint = font_small.render("Connect from the Available tab", True, (100, 130, 200))
        screen.blit(msg,  (card_r.centerx - msg.get_width()  // 2, card_r.y + 28))
        screen.blit(hint, (card_r.centerx - hint.get_width() // 2, card_r.y + 52))

    # Scroll arrows
    list_bottom = _WIFI_LIST_TOP + _WIFI_ROWS_VISIBLE * _WIFI_ROW_H
    if scroll > 0:
        up_r = pygame.Rect(SCREEN_W // 2 - 22, _WIFI_LIST_TOP - 16, 44, 14)
        pygame.draw.rect(screen, (20, 55, 120), up_r, border_radius=4)
        screen.blit(font_small.render("  ▲", True, TEXT_COLOR), (up_r.x + 4, up_r.y + 1))
        _new_rects["WIFI_SAVED_SCROLL_UP"] = up_r
    if scroll + _WIFI_ROWS_VISIBLE < len(nets):
        dn_r = pygame.Rect(SCREEN_W // 2 - 22, list_bottom + 2, 44, 14)
        pygame.draw.rect(screen, (20, 55, 120), dn_r, border_radius=4)
        screen.blit(font_small.render("  ▼", True, TEXT_COLOR), (dn_r.x + 4, dn_r.y + 1))
        _new_rects["WIFI_SAVED_SCROLL_DN"] = dn_r


# ── Password entry overlay ────────────────────────────────────
_WIFI_PW_QWERTY_ALPHA = [
    ["q","w","e","r","t","y","u","i","o","p"],
    ["a","s","d","f","g","h","j","k","l"],
    ["z","x","c","v","b","n","m","BKSP"],
    ["?123","SPACE","CONNECT"],
]

_WIFI_PW_QWERTY_SYM = [
    ["1","2","3","4","5","6","7","8","9","0"],
    ["!","@","#","$","%","^","&","*","(",")",],
    ["-","_","=","+","[","]","{","}",";","BKSP"],
    ["ABC","SPACE","CONNECT"],
]

_WIFI_PW_QWERTY = _WIFI_PW_QWERTY_ALPHA
_wifi_pw_sym_mode = False   # symbol layer toggle for WiFi password

def _draw_wifi_password_overlay() -> None:
    """Modal password-entry overlay – keyboard anchored to bottom, plain-text password."""
    global _wifi_pw_kb_rects, _wifi_pw_sym_mode
    _wifi_pw_kb_rects = {}

    # Layout constants
    PW_KB_KEY_H  = 42
    PW_KB_PAD    = 3
    PW_KB_MARGIN = 4
    PW_KB_W      = SCREEN_W - PW_KB_MARGIN * 2
    # Bottom-anchor: 4 rows of keys + 48-px header bar at the top of the kb panel
    PW_PANEL_TOP = SCREEN_H - (4 * (PW_KB_KEY_H + PW_KB_PAD) + 48 + 4)
    PW_KB_TOP    = PW_PANEL_TOP + 48 + 4

    # Dim background above panel
    dim = pygame.Surface((SCREEN_W, PW_PANEL_TOP), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 180))
    screen.blit(dim, (0, 0))

    # Header area above keyboard
    pygame.draw.rect(screen, (8, 22, 50),
                     pygame.Rect(0, PW_PANEL_TOP, SCREEN_W, SCREEN_H - PW_PANEL_TOP))
    pygame.draw.line(screen, (40, 90, 160), (0, PW_PANEL_TOP), (SCREEN_W, PW_PANEL_TOP), 2)

    # SSID label
    ssid_s = wifi_pw_ssid[:24] + "…" if wifi_pw_ssid and len(wifi_pw_ssid) > 25 else (wifi_pw_ssid or "")
    t = font_title.render(f"Password: {ssid_s}", True, TEXT_COLOR)
    screen.blit(t, (SCREEN_W // 2 - t.get_width() // 2, PW_PANEL_TOP + 6))

    # Password input bar (plain text – no masking)
    pw_bar   = pygame.Rect(PW_KB_MARGIN, PW_PANEL_TOP + 26, SCREEN_W - PW_KB_MARGIN * 2 - 44, 18)
    pygame.draw.rect(screen, (220, 230, 255), pw_bar, border_radius=6)
    pygame.draw.rect(screen, (60, 120, 220), pw_bar, 2, border_radius=6)
    pw_disp  = wifi_pw_text[-26:] if wifi_pw_text else ""
    pw_col   = (20, 20, 40) if wifi_pw_text else (150, 150, 170)
    screen.blit(font_small.render(pw_disp or "Enter password…", True, pw_col),
                (pw_bar.x + 6, pw_bar.y + 3))

    # Cancel button (right of pw bar)
    cancel_r = pygame.Rect(pw_bar.right + 4, PW_PANEL_TOP + 24, 40, 22)
    pygame.draw.rect(screen, (160, 40, 40), cancel_r, border_radius=6)
    cx_s = font_small.render("X", True, TEXT_COLOR)
    screen.blit(cx_s, (cancel_r.centerx - cx_s.get_width() // 2,
                       cancel_r.centery - cx_s.get_height() // 2))
    _wifi_pw_kb_rects["PW_CANCEL"] = cancel_r

    # Choose active layout
    active_rows = _WIFI_PW_QWERTY_SYM if _wifi_pw_sym_mode else _WIFI_PW_QWERTY_ALPHA

    for row_i, row in enumerate(active_rows):
        y = PW_KB_TOP + row_i * (PW_KB_KEY_H + PW_KB_PAD)

        if row_i == 3:
            # Bottom action row: [?123/ABC]  [SPACE]  [CONNECT]
            toggle_key = row[0]   # "?123" or "ABC"
            connect_key = row[2]  # "CONNECT"
            toggle_w  = int(PW_KB_W * 0.18)
            connect_w = int(PW_KB_W * 0.28)
            space_w   = PW_KB_W - toggle_w - connect_w - 2 * PW_KB_PAD
            x = PW_KB_MARGIN

            r = pygame.Rect(x, y, toggle_w, PW_KB_KEY_H)
            _draw_key(r, toggle_key, KEY_SPECIAL, KEY_SPECIAL_LT, font_small)
            _wifi_pw_kb_rects["PW_TOGGLE_SYM"] = r
            x += toggle_w + PW_KB_PAD

            r = pygame.Rect(x, y, space_w, PW_KB_KEY_H)
            _draw_key(r, "SPACE", KEY_SPACE_BG, KEY_SPACE_LT, font_key)
            _wifi_pw_kb_rects["PW_SPACE"] = r
            x += space_w + PW_KB_PAD

            r = pygame.Rect(x, y, connect_w, PW_KB_KEY_H)
            _draw_key(r, "JOIN", KEY_SEARCH, KEY_SEARCH_LT, font_key)
            _wifi_pw_kb_rects["PW_CONNECT"] = r
            continue

        n_keys = len(row)
        # Rows with BKSP at end get special widths
        if "BKSP" in row:
            n_letters  = n_keys - 1
            bksp_ratio = 1.5
            kw     = int((PW_KB_W - n_keys * PW_KB_PAD) / (n_letters + bksp_ratio))
            bksp_w = PW_KB_W - n_letters * kw - n_keys * PW_KB_PAD
            x = PW_KB_MARGIN
            for key in row:
                w = bksp_w if key == "BKSP" else kw
                r = pygame.Rect(x, y, w, PW_KB_KEY_H)
                if key == "BKSP":
                    _draw_key(r, "⌫", KEY_SPECIAL, KEY_SPECIAL_LT, font_key)
                else:
                    _draw_key(r, key.upper() if key.isalpha() else key,
                              KEY_BG, KEY_BG_LIGHT, font_key)
                _wifi_pw_kb_rects[f"PW_{key}"] = r
                x += w + PW_KB_PAD
        else:
            kw      = (PW_KB_W - PW_KB_PAD * (n_keys - 1)) // n_keys
            total_w = kw * n_keys + PW_KB_PAD * (n_keys - 1)
            x       = (SCREEN_W - total_w) // 2
            for key in row:
                r = pygame.Rect(x, y, kw, PW_KB_KEY_H)
                _draw_key(r, key.upper() if key.isalpha() else key,
                          KEY_BG, KEY_BG_LIGHT, font_key)
                _wifi_pw_kb_rects[f"PW_{key}"] = r
                x += kw + PW_KB_PAD


def draw_keyboard() -> None:
    """
    Bottom-anchored keyboard with symbol/alpha toggle.

    Layout (alpha mode):
      Row 0: q w e r t y u i o p   (10 equal keys)
      Row 1: a s d f g h j k l     ( 9 equal, centred)
      Row 2: z x c v b n m  [⌫]   ( 7 letters + wide BKSP)
      Row 3: [?123]  [  SPACE  ]  [SEARCH]

    Symbol mode swaps in QWERTY_ROWS_SYM rows; bottom row becomes [ABC].

    All rects stored in kb_rects so handle_tap() hits the *exact*
    same rectangles that are drawn on screen.
    """
    global kb_rects, kb_sym_mode
    kb_rects = {}

    # Active layout
    active_rows = QWERTY_ROWS_SYM if kb_sym_mode else QWERTY_ROWS_ALPHA

    # ── Panel background (anchored to bottom) ─────────────────────
    panel_top = SCREEN_H - _KB_TOTAL_H
    pygame.draw.rect(screen, (18, 8, 42),
                     pygame.Rect(0, panel_top, SCREEN_W, SCREEN_H - panel_top))

    # ── Search preview bar ────────────────────────────────────────
    CLOSE_W  = 40
    preview  = pygame.Rect(KB_MARGIN, panel_top + 7,
                           SCREEN_W - KB_MARGIN * 2 - CLOSE_W - KB_PAD, 34)
    pygame.draw.rect(screen, (240, 240, 255), preview, border_radius=10)
    pygame.draw.rect(screen, (120, 100, 220), preview, 2, border_radius=10)
    disp  = typed_query[-24:] if len(typed_query) > 25 else typed_query
    disp  = ("…" + disp) if len(typed_query) > 25 else (disp or "Search…")
    col   = SEARCH_TEXT if typed_query else (150, 150, 170)
    screen.blit(font_search.render(disp, True, col),
                (preview.x + 10, preview.y + 8))

    # ── Close button  [✕] ────────────────────────────────────────
    close_r = pygame.Rect(preview.right + KB_PAD, panel_top + 7, CLOSE_W, 34)
    pygame.draw.rect(screen, CLOSE_COLOR,   close_r, border_radius=8)
    pygame.draw.rect(screen, (230, 80, 80), close_r, 2, border_radius=8)
    cl = font_key.render("X", True, TEXT_COLOR)
    screen.blit(cl, (close_r.centerx - cl.get_width() // 2,
                     close_r.centery - cl.get_height() // 2))
    kb_rects["CLOSE"] = close_r

    # ── Key rows ──────────────────────────────────────────────────
    for row_i, row in enumerate(active_rows):
        y = KB_TOP + row_i * (KB_KEY_H + KB_PAD)

        # ── Bottom action row: [?123/ABC]  [SPACE]  [SEARCH] ─────
        if row_i == 3:
            toggle_key = row[0]   # "?123" or "ABC"
            toggle_w   = int(KB_W * 0.18)
            search_w   = int(KB_W * 0.28)
            space_w    = KB_W - toggle_w - search_w - 2 * KB_PAD
            x          = KB_MARGIN

            r = pygame.Rect(x, y, toggle_w, KB_KEY_H)
            _draw_key(r, toggle_key, KEY_SPECIAL, KEY_SPECIAL_LT, font_small)
            kb_rects["TOGGLE_SYM"] = r
            x += toggle_w + KB_PAD

            r = pygame.Rect(x, y, space_w, KB_KEY_H)
            _draw_key(r, "SPACE", KEY_SPACE_BG, KEY_SPACE_LT, font_key)
            kb_rects["SPACE"] = r
            x += space_w + KB_PAD

            r = pygame.Rect(x, y, search_w, KB_KEY_H)
            _draw_key(r, "SEARCH", KEY_SEARCH, KEY_SEARCH_LT, font_key)
            kb_rects["SEARCH"] = r
            continue

        n_keys = len(row)

        # Row with BKSP: give it 1.5× width
        if "BKSP" in row:
            n_letters  = n_keys - 1
            bksp_ratio = 1.5
            kw     = int((KB_W - n_keys * KB_PAD) / (n_letters + bksp_ratio))
            bksp_w = KB_W - n_letters * kw - n_keys * KB_PAD
            x = KB_MARGIN
            for key in row:
                if key == "BKSP":
                    w = bksp_w
                    r = pygame.Rect(x, y, w, KB_KEY_H)
                    _draw_key(r, "⌫", KEY_SPECIAL, KEY_SPECIAL_LT, font_key)
                else:
                    w = kw
                    r = pygame.Rect(x, y, w, KB_KEY_H)
                    _draw_key(r, key.upper() if key.isalpha() else key,
                              KEY_BG, KEY_BG_LIGHT, font_key)
                kb_rects[key] = r
                x += w + KB_PAD

        else:
            # Equal-width keys; centre the row
            kw      = (KB_W - KB_PAD * (n_keys - 1)) // n_keys
            total_w = kw * n_keys + KB_PAD * (n_keys - 1)
            x       = (SCREEN_W - total_w) // 2
            for key in row:
                r = pygame.Rect(x, y, kw, KB_KEY_H)
                _draw_key(r, key.upper() if key.isalpha() else key,
                          KEY_BG, KEY_BG_LIGHT, font_key)
                kb_rects[key] = r
                x += kw + KB_PAD

# ══════════════════════════════════════════════════════════════
# MUSIC HELPERS
# ══════════════════════════════════════════════════════════════
# ── Thumbnail surface lock (written by bg thread, read by render thread) ─────
_thumbnail_lock = threading.Lock()
_play_request_lock = threading.Lock()
_play_request_id = 0

# ── Shared-state lock — guards playlist, history, seen_video_ids, current_song,
#    is_playing, current_time, duration, loading, loading_message, last_fetch_time
_state_lock = threading.Lock()

def _is_latest_play_request(req_id: int) -> bool:
    with _play_request_lock:
        return req_id == _play_request_id

def load_thumbnail(url: str) -> None:
    global thumbnail_surf
    if not url:
        with _thumbnail_lock:
            thumbnail_surf = None
        return
    try:
        r    = requests.get(url, timeout=5)
        r.raise_for_status()
        surf = pygame.image.load(io.BytesIO(r.content))
        new_surf = pygame.transform.smoothscale(surf, (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        with _thumbnail_lock:
            thumbnail_surf = new_surf
    except Exception as e:
        print("Thumbnail failed:", e)
        with _thumbnail_lock:
            thumbnail_surf = None

def fetch_more_songs() -> None:
    global last_fetch_time
    with _state_lock:
        if not current_song:
            return
        if (time.monotonic() - last_fetch_time) < FETCH_COOLDOWN:
            return
        seed_video_id = current_song.get("videoId")
        if not seed_video_id:
            return
        last_fetch_time = time.monotonic()   # stamp before thread so cooldown is respected
    threading.Thread(target=_fetch_more_songs_worker, args=(seed_video_id,), daemon=True).start()

def _fetch_more_songs_worker(seed_video_id: str) -> None:
    print("🔄 Fetching more similar songs…")
    try:
        watch = ytmusic.get_watch_playlist(videoId=seed_video_id, limit=25)
        added = 0
        for tr in watch.get("tracks", [])[1:]:
            # Discard stale auto-fetch results if the user switched songs.
            with _state_lock:
                if not current_song or current_song.get("videoId") != seed_video_id:
                    print("ℹ Auto-fetch aborted (song changed)")
                    return
                vid = tr.get("videoId")
                if vid and vid not in seen_video_ids:
                    playlist.append({
                        "videoId": vid,
                        "title"  : tr.get("title", "Unknown"),
                        "artist" : tr.get("artists", [{}])[0].get("name", "?"),
                    })
                    seen_video_ids.add(vid)
                    added += 1
            if added >= 12:
                break
        with _state_lock:
            prefetch_ids = [s["videoId"] for s in playlist[:3]]
        print(f"✅ Auto-added {added} songs | Queue: {len(playlist)}")
        for vid in prefetch_ids:
            prefetch_stream(vid)
    except Exception as e:
        print("Auto-fetch failed:", e)

def play_new_song(video_id: str,
                  load_initial_playlist: bool = False,
                  append_to_history: bool = True) -> None:
    """Start playing video_id. All blocking I/O runs in a background thread."""
    global loading, loading_message, thumbnail_surf, _play_request_id
    # Clear the old thumbnail immediately on the main thread — before the worker
    # even starts — so it never renders behind the new song's loading state.
    with _thumbnail_lock:
        thumbnail_surf = None
    with _play_request_lock:
        _play_request_id += 1
        req_id = _play_request_id
    with _state_lock:
        loading         = True
        loading_message = "Loading…"
    threading.Thread(
        target=_play_new_song_worker,
        args=(req_id, video_id, load_initial_playlist, append_to_history),
        daemon=True,
    ).start()

def _play_new_song_worker(req_id: int,
                          video_id: str,
                          load_initial_playlist: bool,
                          append_to_history: bool) -> None:
    global current_song, is_playing, current_time, duration
    global loading, loading_message

    if not _is_latest_play_request(req_id):
        return
    player.stop()
    time.sleep(0.1)

    with _state_lock:
        if append_to_history and current_song:
            history.append(current_song.copy())
            if len(history) > MAX_HISTORY:
                old = history.pop(0)
                seen_video_ids.discard(old["videoId"])

    # ── Fetch metadata AND stream URL in parallel ──────────────
    meta_result   = {}
    stream_result = {}

    def _fetch_meta():
        try:
            info   = ytmusic.get_song(video_id)
            vd     = info["videoDetails"]
            meta_result["title"]  = vd["title"]
            meta_result["artist"] = vd["author"]
            meta_result["thumb"]  = vd["thumbnail"]["thumbnails"][-1]["url"]
        except Exception as e:
            print("Metadata fetch failed:", e)

    def _fetch_stream():
        stream_result["url"] = get_stream_url(video_id)

    meta_thread   = threading.Thread(target=_fetch_meta,   daemon=True)
    stream_thread = threading.Thread(target=_fetch_stream, daemon=True)
    meta_thread.start()
    stream_thread.start()
    meta_thread.join()
    stream_thread.join()

    if not _is_latest_play_request(req_id):
        return

    title  = meta_result.get("title",  "Unknown")
    artist = meta_result.get("artist", "Unknown")
    thumb  = meta_result.get("thumb",  None)

    # Kick off thumbnail fetch in background — no need to wait for it
    if thumb:
        threading.Thread(target=load_thumbnail, args=(thumb,), daemon=True).start()

    with _state_lock:
        loading_message = "Buffering…"

    stream_url = stream_result.get("url")
    if not stream_url:
        print(f"❌ No stream for {video_id} — keeping current song")
        if _is_latest_play_request(req_id):
            with _state_lock:
                loading = False
                loading_message = ""
        if player.get_state() != vlc.State.Playing:
            try:
                player.play()
            except Exception:
                pass
        return

    if not _is_latest_play_request(req_id):
        return

    with _state_lock:
        current_song = {
            "videoId"  : video_id,
            "title"    : title,
            "artist"   : artist,
            "thumbnail": thumb,
        }
        seen_video_ids.add(video_id)

    # Thumbnail thread already running from above — no second spawn needed

    try:
        media = vlc_instance.media_new(stream_url)
        player.set_media(media)
        player.play()
        with _state_lock:
            is_playing   = True
            current_time = 0.0
            duration     = 0.0
        print(f"▶ Now playing: {title[:60]}")
    except Exception as e:
        print("Playback failed:", e)
        with _state_lock:
            is_playing = False

    if _is_latest_play_request(req_id):
        with _state_lock:
            loading         = False
            loading_message = ""

    if load_initial_playlist:
        with _state_lock:
            playlist.clear()
            seen_video_ids.clear()
            seen_video_ids.add(video_id)
        try:
            watch = ytmusic.get_watch_playlist(videoId=video_id, limit=18)
            new_tracks = []
            for tr in watch.get("tracks", [])[1:]:
                vid = tr.get("videoId")
                with _state_lock:
                    already_seen = vid in seen_video_ids if vid else True
                if vid and not already_seen:
                    entry = {
                        "videoId": vid,
                        "title"  : tr.get("title", "Unknown"),
                        "artist" : tr.get("artists", [{}])[0].get("name", "?"),
                    }
                    with _state_lock:
                        playlist.append(entry)
                        seen_video_ids.add(vid)
                    new_tracks.append(vid)
            print(f"→ Loaded {len(new_tracks)} next tracks")
            for vid in new_tracks[:3]:
                prefetch_stream(vid)
        except Exception as e:
            print("Initial playlist failed:", e)

def play_next() -> None:
    with _state_lock:
        if not playlist:
            return
        next_song = playlist.pop(0)
    play_new_song(next_song["videoId"], append_to_history=True)

def play_previous() -> None:
    with _state_lock:
        if not history:
            print("No history")
            return
        prev = history.pop()
        if current_song:
            playlist.insert(0, {k: current_song[k] for k in ("videoId", "title", "artist")})
    play_new_song(prev["videoId"], append_to_history=False)

# ══════════════════════════════════════════════════════════════
# TOUCH HANDLER
# ══════════════════════════════════════════════════════════════
def handle_tap(pos_x: int, pos_y: int) -> None:
    global kb_active, typed_query, search_text, current_time, bt_active, bt_scroll, bt_saved_scroll, bt_tab, _bt_confirm_answer
    global wifi_active, wifi_scroll, wifi_saved_scroll, wifi_tab, wifi_pw_ssid, wifi_pw_text, wifi_pw_security
    global _wifi_pw_sym_mode
    global kb_sym_mode

    print(f"Tap → ({pos_x:3d}, {pos_y:3d})")

    # ── Debug dot ─────────────────────────────────────────────
    if TAP_DEBUG:
        _tap_dots.append((pos_x, pos_y, time.monotonic() + TAP_DOT_TTL))

    # ── Screen-off mode: any double-tap anywhere wakes the display ─
    if not screen_on:
        if _check_wake_double_tap():
            screen_wake()
        return  # swallow all taps while screen is off

    # ── Bluetooth screen ──────────────────────────────────────
    if bt_active:
        for key, rect in bt_screen_rects.items():
            if not rect.collidepoint((pos_x, pos_y)):
                continue
            if key == "CLOSE":
                bt_active    = False
                bt_scroll    = 0
                bt_saved_scroll = 0
            elif key == "POWER":
                # Read live state directly rather than cached bt_power
                # to avoid the race where bt_power hasn't been set yet.
                _show_out = subprocess.run(
                    ["bluetoothctl", "show"], capture_output=True, text=True, timeout=4
                ).stdout
                _on = any("Powered: yes" in l for l in _show_out.splitlines())
                bt_set_power(not _on)
            elif key == "SCAN":
                bt_start_scan()
            elif key == "TAB_AVAILABLE":
                bt_tab = "available"
            elif key == "TAB_SAVED":
                bt_tab = "saved"
            elif key == "SCROLL_UP":
                bt_scroll = max(0, bt_scroll - 1)
            elif key == "SCROLL_DN":
                with _bt_lock:
                    _all = list(bt_devices)
                _avail = [d for d in _all if not d["paired"]]
                bt_scroll = min(bt_scroll + 1, max(0, len(_avail) - _BT_ROWS_VISIBLE))
            elif key == "SAVED_SCROLL_UP":
                bt_saved_scroll = max(0, bt_saved_scroll - 1)
            elif key == "SAVED_SCROLL_DN":
                with _bt_lock:
                    _all = list(bt_devices)
                _saved = [d for d in _all if d["paired"]]
                bt_saved_scroll = min(bt_saved_scroll + 1, max(0, len(_saved) - _BT_ROWS_VISIBLE))
            elif key.startswith("AVAIL_"):
                # Available tab — connect (and pair) an unpaired device
                dev_idx = int(key[6:])
                with _bt_lock:
                    _all = list(bt_devices)
                _avail = [d for d in _all if not d["paired"]]
                if 0 <= dev_idx < len(_avail):
                    bt_connect_device(_avail[dev_idx]["mac"])
                    bt_tab = "saved"   # switch to Saved so user sees progress
            elif key.startswith("SAVED_"):
                # Saved tab — connect / disconnect a paired device
                dev_idx = int(key[6:])
                with _bt_lock:
                    _all = list(bt_devices)
                _saved = [d for d in _all if d["paired"]]
                if 0 <= dev_idx < len(_saved):
                    dev = _saved[dev_idx]
                    if dev["connected"]:
                        bt_disconnect_device(dev["mac"])
                    else:
                        bt_connect_device(dev["mac"])
            elif key.startswith("REM_"):
                # Saved tab — remove (unpair/forget) a device
                dev_idx = int(key[4:])
                with _bt_lock:
                    _all = list(bt_devices)
                _saved = [d for d in _all if d["paired"]]
                if 0 <= dev_idx < len(_saved):
                    bt_remove_device(_saved[dev_idx]["mac"])
            elif key == "BT_CONFIRM_YES":
                # User tapped Confirm on the pairing dialog
                _bt_confirm_answer = True
                _bt_confirm_event.set()
            elif key == "BT_CONFIRM_NO":
                # User tapped Cancel on the pairing dialog
                _bt_confirm_answer = False
                _bt_confirm_event.set()
            break
        return

    # ── WiFi screen ───────────────────────────────────────────
    if wifi_active:
        # Password overlay takes priority
        if wifi_pw_ssid:
            for key, rect in _wifi_pw_kb_rects.items():
                if not rect.collidepoint((pos_x, pos_y)):
                    continue
                if key == "PW_CANCEL":
                    wifi_pw_ssid = None
                    wifi_pw_text = ""
                    _wifi_pw_sym_mode = False
                elif key == "PW_CONNECT":
                    _ssid = wifi_pw_ssid
                    _pw   = wifi_pw_text
                    wifi_pw_ssid = None
                    wifi_pw_text = ""
                    _wifi_pw_sym_mode = False
                    wifi_connect(_ssid, _pw)
                elif key == "PW_BKSP":
                    wifi_pw_text = wifi_pw_text[:-1]
                elif key == "PW_SPACE":
                    wifi_pw_text += " "
                elif key == "PW_TOGGLE_SYM":
                    _wifi_pw_sym_mode = not _wifi_pw_sym_mode
                else:
                    # strip the "PW_" prefix → the character
                    wifi_pw_text += key[3:]
                break
            return

        for key, rect in wifi_screen_rects.items():
            if not rect.collidepoint((pos_x, pos_y)):
                continue
            if key == "WIFI_CLOSE":
                wifi_active      = False
                wifi_scroll      = 0
                wifi_saved_scroll = 0
            elif key == "WIFI_SCAN":
                wifi_start_scan()
            elif key == "WIFI_TAB_AVAILABLE":
                wifi_tab = "available"
            elif key == "WIFI_TAB_SAVED":
                wifi_tab = "saved"
            elif key == "WIFI_SCROLL_UP":
                wifi_scroll = max(0, wifi_scroll - 1)
            elif key == "WIFI_SCROLL_DN":
                with _wifi_lock:
                    _all_w = list(wifi_networks)
                _avail_w = [n for n in _all_w if not n["saved"]]
                wifi_scroll = min(wifi_scroll + 1,
                                  max(0, len(_avail_w) - _WIFI_ROWS_VISIBLE))
            elif key == "WIFI_SAVED_SCROLL_UP":
                wifi_saved_scroll = max(0, wifi_saved_scroll - 1)
            elif key == "WIFI_SAVED_SCROLL_DN":
                with _wifi_lock:
                    _all_w = list(wifi_networks)
                _saved_w = [n for n in _all_w if n["saved"]]
                wifi_saved_scroll = min(wifi_saved_scroll + 1,
                                        max(0, len(_saved_w) - _WIFI_ROWS_VISIBLE))
            elif key.startswith("WIFI_AVAIL_"):
                dev_idx = int(key[11:])
                with _wifi_lock:
                    _all_w = list(wifi_networks)
                _avail_w = [n for n in _all_w if not n["saved"]]
                if 0 <= dev_idx < len(_avail_w):
                    net = _avail_w[dev_idx]
                    if net["security"] == "Open":
                        wifi_connect(net["ssid"])
                        wifi_tab = "saved"
                    else:
                        wifi_pw_ssid     = net["ssid"]
                        wifi_pw_text     = ""
                        wifi_pw_security = net["security"]
                        _wifi_pw_sym_mode = False
            elif key.startswith("WIFI_SAVED_"):
                dev_idx = int(key[11:])
                with _wifi_lock:
                    _all_w = list(wifi_networks)
                _saved_w = [n for n in _all_w if n["saved"]]
                if 0 <= dev_idx < len(_saved_w):
                    net = _saved_w[dev_idx]
                    with _wifi_lock:
                        _conn = wifi_connected_ssid
                    if net["ssid"] == _conn:
                        wifi_disconnect()
                    else:
                        wifi_connect(net["ssid"])
            elif key.startswith("WIFI_REM_"):
                dev_idx = int(key[9:])
                with _wifi_lock:
                    _all_w = list(wifi_networks)
                _saved_w = [n for n in _all_w if n["saved"]]
                if 0 <= dev_idx < len(_saved_w):
                    wifi_forget(_saved_w[dev_idx]["ssid"])
            break
        return

    if kb_active:
        for key, rect in kb_rects.items():
            if not rect.collidepoint((pos_x, pos_y)):
                continue
            if key == "CLOSE":
                kb_active = False
                typed_query = ""
                kb_sym_mode = False
            elif key == "SEARCH":
                search_text = typed_query.strip()
                if search_text:
                    query = search_text   # capture for thread closure
                    def _do_search(q):
                        try:
                            res = ytmusic.search(q, filter="songs", limit=1)
                            if res and "videoId" in res[0]:
                                play_new_song(res[0]["videoId"],
                                              load_initial_playlist=True,
                                              append_to_history=False)
                            else:
                                print("Search returned no results")
                        except Exception as e:
                            print("Search failed:", e)
                    threading.Thread(target=_do_search, args=(query,), daemon=True).start()
                kb_active   = False
                typed_query = ""
                kb_sym_mode = False
            elif key == "BKSP":
                typed_query = typed_query[:-1]
            elif key == "SPACE":
                typed_query += " "
            elif key == "TOGGLE_SYM":
                kb_sym_mode = not kb_sym_mode
            else:
                typed_query += key
            break
        return

    # ── Normal (non-keyboard) tap handling ───────────────────
    if THUMBNAIL_RECT.collidepoint((pos_x, pos_y)):
        if _check_sleep_double_tap():
            screen_sleep()
        return  # single tap on thumbnail does nothing else
    elif BT_BTN_RECT.collidepoint((pos_x, pos_y)):
        # Open Bluetooth screen; refresh state in background
        bt_active = True
        bt_scroll = 0
        threading.Thread(target=bt_refresh_devices, daemon=True).start()
    elif WIFI_BTN_RECT.collidepoint((pos_x, pos_y)):
        # Open WiFi screen; refresh state in background
        wifi_active      = True
        wifi_scroll      = 0
        wifi_saved_scroll = 0
        threading.Thread(target=wifi_refresh_networks, daemon=True).start()
    elif SEARCH_RECT.inflate(40, 40).collidepoint((pos_x, pos_y)):
        # Tap on search bar or the old clear-X area opens the keyboard
        kb_active   = True
        typed_query = search_text or ""
    elif PLAY_RECT.collidepoint((pos_x, pos_y)):
        if player.get_state() == vlc.State.Playing:
            player.pause()
        else:
            player.play()
    elif PREV_RECT.collidepoint((pos_x, pos_y)):
        with _state_lock:
            _has_hist = bool(history)
        if _has_hist:
            play_previous()
    elif NEXT_RECT.collidepoint((pos_x, pos_y)):
        with _state_lock:
            _has_next = bool(playlist)
        if _has_next:
            play_next()
    elif SEEK_RECT.collidepoint((pos_x, pos_y)):
        with _state_lock:
            _dur = duration
        if _dur > 0:
            frac = max(0.0, min(1.0, (pos_x - SEEK_RECT.x) / SEEK_RECT.width))
            player.set_time(int(frac * _dur * 1000))
            with _state_lock:
                current_time = frac * _dur
    elif VOL_BAR_RECT.collidepoint((pos_x, pos_y)):
        frac = max(0.0, min(1.0, (pos_x - VOL_BAR_RECT.x) / VOL_BAR_RECT.width))
        set_usb_volume(int(frac * 100))

# ══════════════════════════════════════════════════════════════
# DRAW — MAIN PLAYER UI
# ══════════════════════════════════════════════════════════════
def draw_player() -> None:
    # Snapshot shared state atomically before rendering
    with _state_lock:
        _song     = current_song
        _ct       = current_time
        _dur      = duration
        _vol      = current_volume
        _hist_ok  = bool(history)
        _next_ok  = bool(playlist)

    # Search bar
    pygame.draw.rect(screen, SEARCH_BG, SEARCH_RECT, border_radius=16)
    pygame.draw.rect(screen, (80, 160, 255), SEARCH_RECT, 3, border_radius=16)
    txt_col = SEARCH_TEXT if search_text else TEXT_DIM
    screen.blit(font_search.render(search_text or "Tap to search…", True, txt_col),
                (SEARCH_RECT.x + 12, SEARCH_RECT.y + 8))
    # Small inline ✕ when text is present (right edge of search bar)
    if search_text:
        xs = font_small.render("✕", True, (200, 100, 100))
        screen.blit(xs, (SEARCH_RECT.right - 18, SEARCH_RECT.y + 10))

    # Bluetooth icon button (always visible, colour reflects status)
    _bt_col = _bt_icon_color()
    with _bt_lock:
        _bt_on  = bt_power
        _bt_con = bt_connected_mac is not None
    # Button background pill
    btn_bg = (20, 10, 55) if not _bt_on else ((15, 55, 30) if _bt_con else (15, 30, 75))
    pygame.draw.rect(screen, btn_bg, BT_BTN_RECT, border_radius=8)
    pygame.draw.rect(screen, _bt_col, BT_BTN_RECT, 2, border_radius=8)
    _draw_bt_symbol(screen,
                    BT_BTN_RECT.centerx, BT_BTN_RECT.centery,
                    10, _bt_col, 2)

    # WiFi icon button (always visible, colour reflects status)
    _wf_col = _wifi_icon_color()
    with _wifi_lock:
        _wf_con = wifi_connected_ssid is not None
    wf_btn_bg = (5, 18, 45) if not _wf_con else (5, 38, 20)
    pygame.draw.rect(screen, wf_btn_bg, WIFI_BTN_RECT, border_radius=8)
    pygame.draw.rect(screen, _wf_col, WIFI_BTN_RECT, 2, border_radius=8)
    _draw_wifi_symbol(screen,
                      WIFI_BTN_RECT.centerx, WIFI_BTN_RECT.centery + 3,
                      9, _wf_col, 2)

    # Thumbnail
    pygame.draw.rect(screen, (40, 40, 70), THUMBNAIL_RECT, border_radius=16)
    with _thumbnail_lock:
        _thumb = thumbnail_surf
    if _thumb:
        screen.blit(_thumb, THUMBNAIL_RECT.topleft)
    else:
        note = font_title.render("♪", True, (220, 220, 255))
        screen.blit(note, (THUMBNAIL_RECT.centerx - 20, THUMBNAIL_RECT.centery - 30))

    # Title
    title  = _song["title"]  if _song else "No song playing"
    artist = _song["artist"] if _song else ""
    t_surf = font_title.render(title, True, TEXT_COLOR)
    max_w  = TITLE_RECT.width - 20
    if t_surf.get_width() > max_w:
        scale  = max_w / t_surf.get_width()
        t_surf = pygame.transform.smoothscale(
            t_surf, (int(t_surf.get_width() * scale), int(t_surf.get_height() * scale)))
    screen.blit(t_surf,  (TITLE_RECT.x + 10,  TITLE_RECT.y + 4))
    screen.blit(font_artist.render(artist, True, TEXT_DIM),
                (ARTIST_RECT.x + 10, ARTIST_RECT.y + 2))

    # Progress bar
    pygame.draw.rect(screen, (100, 90, 160), SEEK_RECT, border_radius=4)
    if _dur > 0:
        w = (_ct / _dur) * SEEK_RECT.width
        pygame.draw.rect(screen, PROGRESS_COLOR,
                         (SEEK_RECT.x, SEEK_RECT.y, w, 8), border_radius=4)
        pygame.draw.circle(screen, (240, 240, 255),
                           (int(SEEK_RECT.x + w), SEEK_RECT.centery), 8)
    timestr = (f"{int(_ct // 60)}:{int(_ct % 60):02d} / "
               f"{int(_dur // 60)}:{int(_dur % 60):02d}")
    screen.blit(font_time.render(timestr, True, TEXT_DIM),
                (TIME_RECT.x + 8, TIME_RECT.y))

    # Volume bar
    screen.blit(font_small.render("VOL", True, TEXT_DIM), (VOL_RECT.x + 4, VOL_RECT.y + 1))
    pygame.draw.rect(screen, (100, 90, 160), VOL_BAR_RECT, border_radius=4)
    vw = int((_vol / 100) * VOL_BAR_RECT.width)
    pygame.draw.rect(screen, PROGRESS_COLOR,
                     (VOL_BAR_RECT.x, VOL_BAR_RECT.y, vw, VOL_BAR_RECT.height), border_radius=4)
    pygame.draw.circle(screen, (240, 240, 255),
                       (int(VOL_BAR_RECT.x + vw), VOL_BAR_RECT.centery), 6)

    # Transport controls
    playing    = player.get_state() == vlc.State.Playing
    prev_col   = TEXT_COLOR if _hist_ok else TEXT_DIM
    next_col   = TEXT_COLOR if _next_ok else TEXT_DIM
    play_icon  = "||" if playing else "▶"
    screen.blit(font_ctrl.render("◄◄",      True, prev_col),
                (PREV_RECT.centerx - 22, PREV_RECT.centery - 18))
    screen.blit(font_ctrl.render(play_icon, True, TEXT_COLOR),
                (PLAY_RECT.centerx - 16, PLAY_RECT.centery - 18))
    screen.blit(font_ctrl.render("►►",      True, next_col),
                (NEXT_RECT.centerx - 22, NEXT_RECT.centery - 18))

def draw_mini_now_playing() -> None:
    """Compact now-playing strip shown above the keyboard."""
    with _state_lock:
        _song = current_song
    if not _song:
        return
    mini = _song["title"]
    if len(mini) > 30:
        mini = mini[:29] + "…"
    screen.blit(font_small.render(mini, True, TEXT_DIM), (8, 8))
    pi = "|| " if player.get_state() == vlc.State.Playing else "▶ "
    screen.blit(font_small.render(pi + _song["artist"], True, TEXT_DIM), (8, 26))

# ══════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════
clock = pygame.time.Clock()
set_usb_volume(25)

# ── Bluetooth startup — power on by default ───────────────────
# Run in background so it never delays the first frame.
# bt_set_power handles rfkill unblock + service restart if needed.
threading.Thread(target=lambda: bt_set_power(True), daemon=True).start()

try:
    while True:
        # ── Pygame events ─────────────────────────────────────
        for event in pygame.event.get():
            if event.type == QUIT:
                raise SystemExit

        # ── Touch polling ─────────────────────────────────────
        if touch_dev:
            while True:
                try:
                    ev = touch_dev.read_one()
                except Exception:
                    break
                if ev is None:
                    break

                if ev.type == ecodes.EV_ABS:
                    c = ev.code
                    if   c == ecodes.ABS_X:              touch_state["x"]    = ev.value
                    elif c == ecodes.ABS_Y:              touch_state["y"]    = ev.value
                    elif c == ecodes.ABS_MT_POSITION_X:  touch_state["mt_x"] = ev.value
                    elif c == ecodes.ABS_MT_POSITION_Y:  touch_state["mt_y"] = ev.value
                    elif c == ecodes.ABS_PRESSURE:
                        if ev.value > 0 and not touch_state["touching"]:
                            touch_state["touching"] = True
                            # Arm tap for SYN_REPORT so X/Y are fresh.
                            touch_state["just_pressed"] = True
                        elif ev.value == 0:
                            touch_state["touching"] = False
                            touch_state["just_pressed"] = False

                elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                    if ev.value == 1:
                        touch_state["touching"] = True
                        touch_state["just_pressed"] = True
                    else:
                        touch_state["touching"] = False
                        touch_state["just_pressed"] = False

                elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
                    if touch_state["just_pressed"]:
                        touch_state["just_pressed"] = False
                        rx = touch_state["mt_x"] if _use_mt else touch_state["x"]
                        ry = touch_state["mt_y"] if _use_mt else touch_state["y"]
                        handle_tap(*transform_touch(rx, ry))

        # ── Playback monitoring ───────────────────────────────
        with _state_lock:
            _snap_song    = current_song
            _snap_loading = loading
        if _snap_song and not _snap_loading:
            state = player.get_state()
            if state == vlc.State.Playing:
                with _state_lock:
                    is_playing   = True
                    current_time = player.get_time() / 1000.0
                    if duration == 0:
                        live_dur = player.get_length()
                        if live_dur > 1000:
                            duration = live_dur / 1000.0
                            print(f"⏱ Duration: {duration:.1f}s")
            elif state == vlc.State.Paused:
                with _state_lock:
                    is_playing = False
            elif state in (vlc.State.Ended, vlc.State.Stopped):
                with _state_lock:
                    _was_playing = is_playing
                    is_playing   = False
                if _was_playing:
                    with _state_lock:
                        _has_next = bool(playlist)
                    if _has_next:
                        play_next()

        # ── Queue maintenance ─────────────────────────────────
        with _state_lock:
            _snap_song     = current_song
            _playlist_len  = len(playlist)
            _next_prefetch = playlist[0]["videoId"] if playlist else None
        if _snap_song and _playlist_len <= 3:
            fetch_more_songs()
        if _next_prefetch:
            prefetch_stream(_next_prefetch)

        # ── Render ────────────────────────────────────────────
        if not screen_on:
            clock.tick(FPS)
            continue  # skip all drawing while blanked

        # Always draw the gradient first, then draw the active screen on top.
        # No screen function calls draw_gradient_bg() internally — this is
        # the single authoritative place it is called each frame.
        draw_gradient_bg()

        if bt_active:
            draw_bluetooth_screen()
        elif wifi_active:
            draw_wifi_screen()
        elif kb_active:
            draw_mini_now_playing()
            draw_keyboard()
        else:
            draw_player()

        # ── Tap debug overlay ─────────────────────────────────
        if TAP_DEBUG and _tap_dots:
            now = time.monotonic()
            still = []
            for dx, dy, exp in _tap_dots:
                if now < exp:
                    pygame.draw.circle(screen, (255, 50, 50), (dx, dy), 6)
                    pygame.draw.circle(screen, (255, 255, 255), (dx, dy), 6, 1)
                    coord_s = font_small.render(f"{dx},{dy}", True, (255, 220, 50))
                    screen.blit(coord_s, (min(dx + 8, SCREEN_W - 50), max(dy - 10, 0)))
                    still.append((dx, dy, exp))
            _tap_dots[:] = still

        surface_to_fb(screen)
        clock.tick(FPS)

finally:
    player.stop()
    _fb.close()
    pygame.quit()
    sys.exit(0)
