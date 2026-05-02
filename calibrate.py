"""
Touch Calibration — ADS7846 / XPT2046
Renders directly to /dev/fb1 (320x480 RGB565) via pygame + SDL dummy driver.
No X11 required.

Usage:
    python calibrate.py

Saves result to ~/touch_cal.json, loaded automatically by rpi.py.
"""

import json
import os
import sys
import time
import threading

import numpy as np

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import pygame

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    sys.exit("evdev not found -- run: pip install evdev")

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
SCREEN_W  = 320
SCREEN_H  = 480
FB_PATH   = "/dev/fb1"
SAVE_PATH = os.path.expanduser("~/touch_cal.json")
FPS       = 30
SAMPLES_PER_TARGET = 5
CAL_EDGE_PAD = 24

# Crosshair target inset from each corner (px)
INSET = 0

# ── Colours ──────────────────────────────────────────────────
BG          = ( 18,  12,  40)
BG2         = ( 38,  22,  80)
C_TARGET    = (255, 255, 255)
C_TARGET_DIM= ( 80,  80, 120)
C_DONE      = ( 60, 200, 100)
C_ACTIVE    = (255, 210,  50)
C_PULSE     = (255, 120,  50)
C_TEXT      = (220, 220, 255)
C_DIM       = (130, 130, 170)
C_BAR_BG    = ( 50,  40,  90)
C_BAR_FG    = ( 90, 160, 255)
C_SUCCESS   = ( 60, 200, 100)
C_PANEL     = ( 30,  20,  65)

TOUCH_KEYWORDS = ["xpt2046", "ads7846", "ft5", "goodix", "touch", "pen"]

# ══════════════════════════════════════════════════════════════
# FRAMEBUFFER WRITE
# ══════════════════════════════════════════════════════════════
_fb = open(FB_PATH, "wb")

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
# TOUCH DEVICE
# ══════════════════════════════════════════════════════════════
def find_touch_device():
    for path in list_devices():
        try:
            dev = InputDevice(path)
            if any(kw in dev.name.lower() for kw in TOUCH_KEYWORDS):
                return dev
        except Exception:
            continue
    for fallback in ["/dev/input/event1", "/dev/input/event0"]:
        try:
            return InputDevice(fallback)
        except Exception:
            continue
    return None

def drain(device):
    while True:
        try:
            e = device.read_one()
            if e is None:
                break
        except Exception:
            break

def read_touch(device):
    """
    Block until a full press+release cycle. Returns (raw_x, raw_y).
    State machine: IDLE -> PRESSED (on down) -> return (on up).
    """
    IDLE = 0; PRESSED = 1
    state = IDLE
    x = y = None
    while True:
        try:
            ev = device.read_one()
        except Exception:
            time.sleep(0.01); continue
        if ev is None:
            time.sleep(0.01); continue

        if ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
            if ev.value == 1:
                state = PRESSED; x = y = None
            elif ev.value == 0 and state == PRESSED:
                if x is not None and y is not None:
                    return x, y
                state = IDLE

        elif ev.type == ecodes.EV_ABS and ev.code == ecodes.ABS_PRESSURE:
            if ev.value > 0 and state == IDLE:
                state = PRESSED; x = y = None
            elif ev.value == 0 and state == PRESSED:
                if x is not None and y is not None:
                    return x, y
                state = IDLE

        elif ev.type == ecodes.EV_ABS and state == PRESSED:
            if ev.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X):
                x = ev.value
            elif ev.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y):
                y = ev.value

# ══════════════════════════════════════════════════════════════
# DRAW HELPERS
# ══════════════════════════════════════════════════════════════
def draw_crosshair(surf, cx, cy, color, size=22, thickness=2):
    """Draw a + crosshair with a hollow circle."""
    # arms
    pygame.draw.line(surf, color, (cx - size, cy), (cx - 8, cy), thickness)
    pygame.draw.line(surf, color, (cx + 8, cy), (cx + size, cy), thickness)
    pygame.draw.line(surf, color, (cx, cy - size), (cx, cy - 8), thickness)
    pygame.draw.line(surf, color, (cx, cy + 8), (cx, cy + size), thickness)
    # circle
    pygame.draw.circle(surf, color, (cx, cy), 8, thickness)

def draw_checkmark(surf, cx, cy, color, size=14):
    """Draw a tick mark."""
    pygame.draw.lines(surf, color, False, [
        (cx - size // 2, cy),
        (cx - size // 6, cy + size // 2),
        (cx + size // 2, cy - size // 3),
    ], 3)

def draw_gradient_bg(surf):
    for y in range(SCREEN_H):
        t = y / SCREEN_H
        r = int(BG[0] + (BG2[0] - BG[0]) * t)
        g = int(BG[1] + (BG2[1] - BG[1]) * t)
        b = int(BG[2] + (BG2[2] - BG[2]) * t)
        pygame.draw.line(surf, (r, g, b), (0, y), (SCREEN_W, y))

def draw_progress_bar(surf, step, total, y=440):
    """Draw a thin progress bar near the bottom."""
    bar_x = 20
    bar_w = SCREEN_W - 40
    bar_h = 6
    pygame.draw.rect(surf, C_BAR_BG, (bar_x, y, bar_w, bar_h), border_radius=3)
    filled = int(bar_w * step / total)
    if filled > 0:
        pygame.draw.rect(surf, C_BAR_FG, (bar_x, y, filled, bar_h), border_radius=3)

def draw_panel(surf, rect, color=C_PANEL, alpha=200):
    """Draw a semi-transparent rounded panel."""
    s = pygame.Surface((rect[2], rect[3]), pygame.SRCALPHA)
    s.fill((*color, alpha))
    pygame.draw.rect(s, (*C_DIM, 120), (0, 0, rect[2], rect[3]), 1, border_radius=10)
    surf.blit(s, (rect[0], rect[1]))

# ══════════════════════════════════════════════════════════════
# CALIBRATION POINTS  (screen pixel positions of each target)
# ══════════════════════════════════════════════════════════════
TARGETS = [
    ("TOP-LEFT",     INSET,            INSET),
    ("TOP-RIGHT",    SCREEN_W - INSET, INSET),
    ("BOTTOM-RIGHT", SCREEN_W - INSET, SCREEN_H - INSET),
    ("BOTTOM-LEFT",  INSET,            SCREEN_H - INSET),
]


def _compute_calibration(samples_by_label):
    """Build stable calibration bounds from corner averages."""
    corner_means = {}
    for label, _, _ in TARGETS:
        pts = samples_by_label.get(label, [])
        if not pts:
            raise ValueError(f"No samples captured for {label}")
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        corner_means[label] = {
            "x": int(round(float(np.mean(xs)))),
            "y": int(round(float(np.mean(ys)))),
        }

    # Use full-corner span, not side-averaged axes. Side averaging can collapse
    # bounds on rotated touch mappings where left/right are split across corners.
    corner_x = [v["x"] for v in corner_means.values()]
    corner_y = [v["y"] for v in corner_means.values()]
    left_x = min(corner_x)
    right_x = max(corner_x)
    top_y = min(corner_y)
    bottom_y = max(corner_y)

    return {
        "TOUCH_X_MIN": max(0, left_x - CAL_EDGE_PAD),
        "TOUCH_X_MAX": min(4095, right_x + CAL_EDGE_PAD),
        "TOUCH_Y_MIN": max(0, top_y - CAL_EDGE_PAD),
        "TOUCH_Y_MAX": min(4095, bottom_y + CAL_EDGE_PAD),
        "CAL_VERSION": 2,
        "SAMPLES_PER_TARGET": SAMPLES_PER_TARGET,
        "EDGE_PAD": CAL_EDGE_PAD,
        "CORNER_AVERAGES": corner_means,
    }

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    dev = find_touch_device()
    if not dev:
        sys.exit("No touch device found.")

    pygame.init()
    screen = pygame.Surface((SCREEN_W, SCREEN_H))
    font_lg = pygame.font.SysFont("arial", 22, bold=True)
    font_md = pygame.font.SysFont("arial", 16)
    font_sm = pygame.font.SysFont("arial", 13)
    clock  = pygame.time.Clock()

    total_targets = len(TARGETS)
    total_taps = total_targets * SAMPLES_PER_TARGET
    all_taps = []
    samples_by_label = {label: [] for label, _, _ in TARGETS}

    # ── Touch reading happens in a background thread so the
    #    main loop keeps rendering the pulsing animation.
    touch_result = {}
    touch_event  = threading.Event()

    def touch_worker():
        drain(dev)
        x, y = read_touch(dev)
        touch_result["x"] = x
        touch_result["y"] = y
        touch_event.set()

    # ── Screens: INTRO -> CALIBRATE -> DONE
    phase      = "INTRO"
    target_idx = 0
    sample_idx = 0
    pulse_t    = 0.0
    done_since = None
    final_cal = None

    # kick off first touch wait immediately after intro dismissed
    waiting_touch = False

    def start_waiting():
        nonlocal waiting_touch
        touch_result.clear()
        touch_event.clear()
        waiting_touch = True
        threading.Thread(target=touch_worker, daemon=True).start()

    # ── Check for any touch to dismiss intro (reuse touch worker) ─
    intro_event = threading.Event()
    def intro_touch_worker():
        drain(dev)
        read_touch(dev)
        intro_event.set()
    threading.Thread(target=intro_touch_worker, daemon=True).start()

    while True:
        dt = clock.tick(FPS) / 1000.0
        pulse_t += dt

        # ── Events ───────────────────────────────────────────
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                pygame.quit(); _fb.close(); sys.exit()

        # ── Phase transitions ────────────────────────────────
        if phase == "INTRO" and intro_event.is_set():
            phase = "CALIBRATE"
            start_waiting()

        elif phase == "CALIBRATE" and waiting_touch and touch_event.is_set():
            rx, ry = touch_result["x"], touch_result["y"]
            label = TARGETS[target_idx][0]
            samples_by_label[label].append((rx, ry))
            all_taps.append((rx, ry))
            waiting_touch = False

            sample_idx += 1
            if sample_idx >= SAMPLES_PER_TARGET:
                sample_idx = 0
                target_idx += 1

            if target_idx >= total_targets:
                final_cal = _compute_calibration(samples_by_label)
                phase = "DONE"
                done_since = time.monotonic()
            else:
                time.sleep(0.35)   # brief pause between targets
                start_waiting()

        elif phase == "DONE" and done_since and (time.monotonic() - done_since) > 2.5:
            break   # auto-exit after showing result

        # ── Draw ─────────────────────────────────────────────
        draw_gradient_bg(screen)

        if phase == "INTRO":
            _draw_intro(screen, font_lg, font_md, font_sm, pulse_t)

        elif phase == "CALIBRATE":
            _draw_calibrate(screen, font_lg, font_md, font_sm,
                            target_idx, total_targets,
                            sample_idx, SAMPLES_PER_TARGET,
                            len(all_taps), total_taps, pulse_t)

        elif phase == "DONE":
            _draw_done(screen, font_lg, font_md, font_sm, final_cal, pulse_t)

        surface_to_fb(screen)

    # ── Save ─────────────────────────────────────────────────
    cal = final_cal if final_cal else _compute_calibration(samples_by_label)
    with open(SAVE_PATH, "w") as f:
        json.dump(cal, f, indent=2)

    # ── Final blank ───────────────────────────────────────────
    pygame.quit()
    _fb.close()
    print(f"Calibration saved to {SAVE_PATH}")
    for k, v in cal.items():
        print(f"  {k} = {v}")


# ══════════════════════════════════════════════════════════════
# DRAW PHASES
# ══════════════════════════════════════════════════════════════
def _draw_intro(surf, font_lg, font_md, font_sm, t):
    cx = SCREEN_W // 2

    # Title
    title = font_lg.render("Touch Calibration", True, C_TEXT)
    surf.blit(title, (cx - title.get_width() // 2, 60))

    # Icon — big crosshair in the centre
    pulse = 0.5 + 0.5 * abs(__import__("math").sin(t * 2.5))
    col = tuple(int(C_TARGET_DIM[i] + (C_ACTIVE[i] - C_TARGET_DIM[i]) * pulse) for i in range(3))
    draw_crosshair(surf, cx, 200, col, size=36, thickness=2)

    # Instructions panel
    draw_panel(surf, (20, 290, SCREEN_W - 40, 130))
    lines = [
        "Tap each corner",
        f"{SAMPLES_PER_TARGET} times.",
        "",
        "Hold briefly, then",
        "lift your finger.",
    ]
    for i, line in enumerate(lines):
        s = font_md.render(line, True, C_TEXT if line else C_DIM)
        surf.blit(s, (cx - s.get_width() // 2, 302 + i * 22))

    # Tap to start
    alpha = int(160 + 95 * abs(__import__("math").sin(t * 2.0)))
    tap = font_sm.render("Tap anywhere to begin", True, (*C_DIM, alpha)[:3])
    surf.blit(tap, (cx - tap.get_width() // 2, 455))


def _draw_calibrate(surf, font_lg, font_md, font_sm,
                    target_idx, total_targets,
                    sample_idx, samples_per_target,
                    taps_done, taps_total, t):
    import math
    cx = SCREEN_W // 2

    label, tx, ty = TARGETS[target_idx]

    # Draw all previous targets as done checkmarks
    for i, (lbl, px, py) in enumerate(TARGETS):
        if i < target_idx:
            draw_crosshair(surf, px, py, C_DONE, size=18, thickness=2)
            draw_checkmark(surf, px, py, C_DONE)
        elif i == target_idx:
            # Pulsing active target
            pulse = 0.5 + 0.5 * math.sin(t * 4.0)
            outer_col = tuple(int(C_TARGET_DIM[j] + (C_ACTIVE[j] - C_TARGET_DIM[j]) * pulse) for j in range(3))
            inner_col = tuple(int(C_ACTIVE[j] + (C_PULSE[j] - C_ACTIVE[j]) * pulse) for j in range(3))
            draw_crosshair(surf, px, py, outer_col, size=26, thickness=2)
            # small filled dot at centre
            dot_r = max(2, int(4 + 2 * pulse))
            pygame.draw.circle(surf, inner_col, (px, py), dot_r)
        else:
            draw_crosshair(surf, px, py, C_TARGET_DIM, size=18, thickness=1)

    # Centre instruction panel
    panel_y = SCREEN_H // 2 - 55
    draw_panel(surf, (30, panel_y, SCREEN_W - 60, 110))

    step_s = font_lg.render(f"Step {target_idx + 1} of {total_targets}", True, C_ACTIVE)
    surf.blit(step_s, (cx - step_s.get_width() // 2, panel_y + 10))

    lbl_s = font_md.render(label.replace("-", " "), True, C_TEXT)
    surf.blit(lbl_s, (cx - lbl_s.get_width() // 2, panel_y + 42))

    hint = font_sm.render(f"Sample {sample_idx + 1} / {samples_per_target}", True, C_DIM)
    surf.blit(hint, (cx - hint.get_width() // 2, panel_y + 70))

    # Progress bar
    draw_progress_bar(surf, taps_done, taps_total)

    # Step dots
    dot_y = 458
    spacing = 18
    start_x = cx - ((total_targets - 1) * spacing) // 2
    for i in range(total_targets):
        col = C_DONE if i < target_idx else (C_ACTIVE if i == target_idx else C_TARGET_DIM)
        r   = 5 if i == target_idx else 4
        pygame.draw.circle(surf, col, (start_x + i * spacing, dot_y), r)


def _draw_done(surf, font_lg, font_md, font_sm, cal, t):
    import math
    cx = SCREEN_W // 2

    # Big checkmark
    pulse = 0.7 + 0.3 * math.sin(t * 3.0)
    col = tuple(int(C_DONE[i] * pulse) for i in range(3))
    pygame.draw.circle(surf, col, (cx, 110), 40, 3)
    draw_checkmark(surf, cx, 110, col, size=28)

    title = font_lg.render("Calibration Saved!", True, C_SUCCESS)
    surf.blit(title, (cx - title.get_width() // 2, 168))

    draw_panel(surf, (20, 210, SCREEN_W - 40, 160))
    keys = ["TOUCH_X_MIN", "TOUCH_X_MAX", "TOUCH_Y_MIN", "TOUCH_Y_MAX"]
    labels = ["X min", "X max", "Y min", "Y max"]
    for i, (k, lbl) in enumerate(zip(keys, labels)):
        row_y = 222 + i * 34
        lbl_s = font_md.render(lbl, True, C_DIM)
        val_s = font_md.render(str(cal[k]), True, C_TEXT)
        surf.blit(lbl_s, (40, row_y))
        surf.blit(val_s, (SCREEN_W - 40 - val_s.get_width(), row_y))
        if i < 3:
            pygame.draw.line(surf, (*C_DIM, 60)[:3],
                             (40, row_y + 28), (SCREEN_W - 40, row_y + 28), 1)

    saved = font_sm.render(f"Saved to touch_cal.json", True, C_DIM)
    surf.blit(saved, (cx - saved.get_width() // 2, 382))

    alpha_hint = int(140 + 80 * abs(math.sin(t * 1.8)))
    hint = font_sm.render("Closing automatically...", True, C_DIM)
    surf.blit(hint, (cx - hint.get_width() // 2, 455))


if __name__ == "__main__":
    main()