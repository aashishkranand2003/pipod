"""
Music Player — Raspberry Pi Zero 2W
Display : 3.5-inch SPI framebuffer (/dev/fb1, 320×480, RGB565)
Audio   : USB audio output (ALSA card index auto-detected, falls back to card 1)
"""

import io
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
# USB AUDIO — find the correct ALSA card index at startup
# ══════════════════════════════════════════════════════════════
def _find_usb_audio_card() -> int:
    """Return the ALSA card index for the first USB audio device, else 1."""
    try:
        out = subprocess.check_output(["aplay", "-l"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "usb" in line.lower() and line.startswith("card"):
                return int(line.split()[1].rstrip(":"))
    except Exception:
        pass
    return 1  # safe default for most USB DACs on Pi

ALSA_CARD = _find_usb_audio_card()
# Use plughw (not hw) — plughw applies automatic format/rate conversion which
# prevents ALSA xruns and sample-rate mismatch distortion on USB DACs.
ALSA_DEV  = f"plughw:{ALSA_CARD}"
print(f"USB audio card: {ALSA_CARD}  ({ALSA_DEV})")

# ══════════════════════════════════════════════════════════════
# VLC — force output to the USB audio device
# ══════════════════════════════════════════════════════════════
_vlc_args = (
    "--no-video",
    "--quiet",
    "--no-ts-trust-pcr"
    # ── Force USB audio output — no fallback to PulseAudio / HDMI ─
    "--aout=alsa",                      # use ALSA, never PulseAudio
    f"--alsa-audio-device={ALSA_DEV}",  # pin to the USB DAC device
    "--no-sout-keep",                    # don't re-use output across streams
    # ── Audio quality improvements ────────────────────────────
    "--network-caching=3000",       # 3 s network buffer — prevents stutter/glitches
    "--live-caching=3000",          # same for live/adaptive streams
    "--audio-resampler=soxr",       # high-quality SoX resampler (vs. ugly default)
    "--clock-jitter=0",             # disable clock jitter compensation noise
    "--clock-synchro=0",            # disable A/V sync drift that can warp audio
)
vlc_instance = vlc.Instance(*_vlc_args)
player       = vlc_instance.media_player_new()

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
SEARCH_RECT    = pygame.Rect(10, 8, SCREEN_W - 48, 36)
CLEAR_RECT     = pygame.Rect(SCREEN_W - 42, 10, 28, 32)
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
QWERTY_ROWS = [
    ["1","2","3","4","5","6","7","8","9","0"],
    ["q","w","e","r","t","y","u","i","o","p"],
    ["a","s","d","f","g","h","j","k","l"],
    ["z","x","c","v","b","n","m","BKSP"],
    ["SPACE","SEARCH"],
]

KB_MARGIN  = 4          # left/right edge gap (px)
KB_PAD     = 3          # gap between keys (px)
KB_KEY_H   = 40         # key height (px)
KB_TOP     = 260        # y of the first key row
KB_W       = SCREEN_W - KB_MARGIN * 2   # usable keyboard width

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
typed_query     = ""
current_volume  = 25
kb_rects: dict  = {}

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
    global screen_on
    _set_backlight(True)
    screen_on = True      # re-enable rendering before the next frame
    print("🌕 Screen on")

def _check_thumbnail_double_tap(pos_x: int, pos_y: int) -> bool:
    """
    Return True and reset the timer if this tap on the thumbnail area
    is the second tap within DOUBLE_TAP_MAX_GAP seconds.
    Also handles waking from sleep on ANY double-tap anywhere.
    """
    global _last_thumb_tap_time
    now = time.monotonic()
    gap = now - _last_thumb_tap_time
    _last_thumb_tap_time = now
    if gap <= DOUBLE_TAP_MAX_GAP:
        _last_thumb_tap_time = 0.0   # reset so a triple-tap doesn't re-trigger
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
                ["amixer", "-c", str(ALSA_CARD), "-M", "-q",
                 "sset", ctrl, f"{current_volume}%"],
                check=True,
                stderr=subprocess.DEVNULL,
            )
            return          # stop on first success
        except subprocess.CalledProcessError:
            continue

# ══════════════════════════════════════════════════════════════
# DRAW HELPERS
# ══════════════════════════════════════════════════════════════
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


def draw_keyboard() -> None:
    """
    Redesigned keyboard.

    Layout rules
    ────────────
    Rows 0-1 : 10 equal keys  (digits + qwerty top row)
    Row 2    :  9 equal keys  (home row a-l)
    Row 3    :  7 equal letter keys  +  BKSP at 1.5× width
    Row 4    :  SPACE (60 %)  +  SEARCH (40 % – KB_PAD gap)

    All rects stored in kb_rects so handle_tap() hits the *exact*
    same rectangles that are drawn on screen.
    """
    global kb_rects
    kb_rects = {}

    # ── Panel background ──────────────────────────────────────────
    panel_top = KB_TOP - 48
    pygame.draw.rect(screen, (18, 8, 42),
                     pygame.Rect(0, panel_top, SCREEN_W, SCREEN_H - panel_top))

    # ── Search preview bar ────────────────────────────────────────
    CLOSE_W  = 40
    preview  = pygame.Rect(KB_MARGIN, panel_top + 7,
                           SCREEN_W - KB_MARGIN * 2 - CLOSE_W - KB_PAD, 34)
    pygame.draw.rect(screen, (240, 240, 255), preview, border_radius=10)
    # thin accent border
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
    for row_i, row in enumerate(QWERTY_ROWS):
        y = KB_TOP + row_i * (KB_KEY_H + KB_PAD)

        # ── Bottom row: SPACE + SEARCH ────────────────────────────
        if row_i == 4:
            total = KB_W
            search_w = int(total * 0.38)
            space_w  = total - search_w - KB_PAD
            x        = KB_MARGIN

            # SPACE
            r = pygame.Rect(x, y, space_w, KB_KEY_H)
            _draw_key(r, "SPACE", KEY_SPACE_BG, KEY_SPACE_LT, font_key)
            kb_rects["SPACE"] = r
            x += space_w + KB_PAD

            # SEARCH
            r = pygame.Rect(x, y, search_w, KB_KEY_H)
            _draw_key(r, "SEARCH", KEY_SEARCH, KEY_SEARCH_LT, font_key)
            kb_rects["SEARCH"] = r
            continue

        # ── Rows with letter/number keys ─────────────────────────
        # Row 3 (z-m + BKSP): BKSP = 1.5× a normal key
        # Every other row: n equal keys
        n_keys = len(row)

        if row_i == 3:
            # 7 letter keys + 1 BKSP (1.5× wide)
            # total slots = 7 + 1.5 = 8.5  →  key_w = KB_W / 8.5 gaps
            n_letters  = n_keys - 1   # 7
            bksp_ratio = 1.5
            # KB_W = n_letters * kw + (n_letters - 1) * KB_PAD
            #      + bksp_ratio * kw + KB_PAD
            # KB_W = kw * (n_letters + bksp_ratio) + n_keys * KB_PAD
            kw = int((KB_W - n_keys * KB_PAD) / (n_letters + bksp_ratio))
            bksp_w = KB_W - n_letters * kw - n_keys * KB_PAD
            x = KB_MARGIN
            for key in row:
                if key == "BKSP":
                    w = bksp_w
                    r = pygame.Rect(x, y, w, KB_KEY_H)
                    _draw_key(r, "CLR", KEY_SPECIAL, KEY_SPECIAL_LT, font_key)
                else:
                    w = kw
                    r = pygame.Rect(x, y, w, KB_KEY_H)
                    _draw_key(r, key.upper(), KEY_BG, KEY_BG_LIGHT, font_key)
                kb_rects[key] = r
                x += w + KB_PAD

        else:
            # Equal-width keys; centre the row
            kw      = (KB_W - KB_PAD * (n_keys - 1)) // n_keys
            total_w = kw * n_keys + KB_PAD * (n_keys - 1)
            x       = (SCREEN_W - total_w) // 2
            for key in row:
                r = pygame.Rect(x, y, kw, KB_KEY_H)
                _draw_key(r, key.upper(), KEY_BG, KEY_BG_LIGHT, font_key)
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
    global kb_active, typed_query, search_text, current_time

    print(f"Tap → ({pos_x:3d}, {pos_y:3d})")

    # ── Debug dot ─────────────────────────────────────────────
    if TAP_DEBUG:
        _tap_dots.append((pos_x, pos_y, time.monotonic() + TAP_DOT_TTL))

    # ── Screen-off mode: any double-tap anywhere wakes the display ─
    if not screen_on:
        if _check_thumbnail_double_tap(pos_x, pos_y):
            screen_wake()
        return  # swallow all taps while screen is off

    if kb_active:
        for key, rect in kb_rects.items():
            if not rect.collidepoint((pos_x, pos_y)):
                continue
            if key == "CLOSE":
                kb_active = False
                typed_query = ""
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
            elif key == "BKSP":
                typed_query = typed_query[:-1]
            elif key == "SPACE":
                typed_query += " "
            else:
                typed_query += key
            break
        return

    # ── Normal (non-keyboard) tap handling ───────────────────
    if THUMBNAIL_RECT.collidepoint((pos_x, pos_y)):
        if _check_thumbnail_double_tap(pos_x, pos_y):
            screen_sleep()
        return  # single tap on thumbnail does nothing else
    elif SEARCH_RECT.inflate(40, 40).collidepoint((pos_x, pos_y)):
        kb_active   = True
        typed_query = search_text or ""
    elif CLEAR_RECT.collidepoint((pos_x, pos_y)):
        search_text = ""
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
    if search_text:
        screen.blit(font_ctrl.render("X", True, (255, 100, 100)),
                    (CLEAR_RECT.x + 6, CLEAR_RECT.y + 2))

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

        draw_gradient_bg()

        if kb_active:
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
