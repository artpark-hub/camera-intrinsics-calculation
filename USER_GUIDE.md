# MDCT-Proto User Guide

**Mobile Dynamic Calibration Target for RTSP Camera Intrinsic Calibration**

---

## Overview

MDCT-Proto determines the intrinsic parameters (focal length, principal point,
distortion coefficients) of an RTSP camera by using an iPad as a moving
calibration target. You hold the iPad displaying a ChArUco pattern in front of
the camera while the application on your Mac (or Linux/Windows laptop) captures
diverse poses and computes the calibration.

---

## Setup

### 1. Run the setup script

```bash
cd ~/Desktop/ART/3d/calib
chmod +x setup.sh
./setup.sh
```

This installs `uv` (if needed), creates a virtual environment, and installs all
dependencies from `pyproject.toml`. On macOS it also installs the optional
Quartz framework for screen auto-detection.

### 2. Start the application

**Auto-capture mode (recommended):**

```bash
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11
```

**Timed-capture mode:**

```bash
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11 \
    --capture_mode timed --capture_interval 3
```

**Stable mode (legacy — requires holding iPad still):**

```bash
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11 \
    --capture_mode stable
```

### Common CLI options

| Flag | Purpose | Default |
|---|---|---|
| `--rtsp URL` | RTSP stream URL of the camera to calibrate | — |
| `--ipad` | Enable two-device iPad mode | off |
| `--ipad_model MODEL` | iPad model for auto board-width (implies `--ipad`) | — |
| `--capture_mode MODE` | `auto`, `timed`, or `stable` | `auto` |
| `--capture_interval N` | Seconds between captures in timed mode | `2.0` |
| `--board_width_mm N` | Manual board physical width (mm) if model not listed | auto |
| `--min_captures N` | Minimum poses to collect | `20` |
| `--diversity_threshold N` | Diversity score needed to complete (0-1) | `0.35` |
| `--port N` | Web server port | `8080` |
| `--no_voice` | Disable voice guidance | off |
| `--list_devices` | Print all known iPad/tablet models and exit | — |

See `python main.py --help` for the full list.

### 3. Open the pattern on your iPad

On the iPad, open Safari and navigate to:

```
http://<your-laptop-ip>:8080/
```

The application prints the exact URL at startup. Tap the screen to enter
fullscreen. You will see the ChArUco checkerboard pattern fill the display.

### 4. (Optional) Open the feed monitor on another device

To watch the annotated camera feed on a phone or second screen:

```
http://<your-laptop-ip>:8080/monitor
```

---

## What You See on Each Device

| iPad (you hold this) | Mac / Laptop screen |
|---|---|
| ChArUco pattern with a colored border | Live camera feed with HUD overlay |
| Status bar at bottom with instruction + capture count | Quality bars (X, Y, Size, Skew) on the right sidebar |
| | Sector, tilt, and sample count at bottom-right |

---

## The iPad Border — Your Primary Feedback

The colored border around the ChArUco pattern tells you what the camera sees
without having to look at the laptop.

| Border Color | Meaning | What To Do |
|---|---|---|
| **Red, pulsing** | Camera cannot see the board | Keep moving — you are out of frame |
| **Amber, steady** | Camera sees the board | Keep moving slowly (auto mode) or hold still (stable mode) |
| **Green, flash** | Captured! | Move to the next position/angle/distance |

---

## Capture Modes Explained

### Auto Mode (default) — Easiest

Just slowly wave the iPad around in front of the camera. The system:

1. Buffers recent frames as you move
2. Detects when the board enters a new quality region (new position, distance, or tilt)
3. Picks the **sharpest** frame from the buffer (least motion blur)
4. Captures automatically

**You never need to hold still.** Just keep moving and varying your position,
distance, and tilt angle. Captures happen every ~0.8 seconds when you cover
new regions.

### Timed Mode — Simplest

Captures every N seconds (`--capture_interval`, default 2s) whenever the board
is visible. Just keep the board in frame and it captures periodically. No
stability or quality requirements — the outlier rejection handles bad frames.

### Stable Mode — Legacy

Requires holding the iPad still for several frames before capture. This was the
original mode and is the most physically demanding. Use `--capture_mode stable`
if you prefer deliberate, controlled captures.

---

## Voice Prompts — What You Will Hear

### Phase 1: Getting Started

**Auto mode:**
> *"Calibration started. Slowly wave the screen around the camera view.
> Captures happen automatically."*

**Stable mode:**
> *"Calibration started. Move the screen into the camera view."*

### Phase 2: First 5 Captures (lenient)

The system captures any visible pose to give you early positive feedback:

> *"Captured pose 1. 19 more to go."*
> *"Captured pose 2. 18 more to go."*

After **5 captures** the system becomes selective — it will only capture poses
that improve quality coverage.

### Phase 3: Guided Diversity (the main phase)

Every **8 seconds** the system checks which quality axis is weakest and tells
you what to change:

**If Size is low** (same distance):
> *"Change your distance to the camera. Walk closer, then walk farther back."*

**If X position is low** (staying centered):
> *"Step sideways. Move a few steps to your left, then to your right."*

**If Y position is low** (same height):
> *"Change the iPad height. Hold it up high, then down low."*

**If Skew is low** (iPad has been flat):
> *"Tilt the iPad more steeply. Angle it so the top or side is farther
> from the camera."*

When diversity is almost good enough (score >= 0.55), the prompts soften:
> *"A few more at different distances would help."*

### Phase 4: Completion

Once enough diverse samples are collected:

> *"Calibration data collection complete! Computing intrinsics."*

Then after computation:

> *"Calibration finished. Quality is excellent with RMS error of 0.13 pixels."*

---

## The Ideal Routine (Auto Mode)

Think of it as **a slow sweep covering positions, distances, and angles**:

1. **Start close** (~1 m) — sweep the iPad left to right across the camera view
2. **Step back** (~2 m) — sweep again, varying height (hold high, then low)
3. **Step back more** (~3 m) — sweep again
4. **Throughout** — occasionally tilt the iPad (lean it forward, back, left, right)

The system captures automatically as you cover new regions. The voice guidance
tells you what's still needed. A typical calibration takes **2-3 minutes** and
**20 captures**.

---

## Quality Bars (HUD Sidebar)

The Mac screen shows four progress bars on the right side:

| Bar | What It Measures | How To Fill It |
|---|---|---|
| **X pos** | Horizontal position coverage | Move left and right |
| **Y pos** | Vertical position coverage | Move up and down |
| **Size** | Board size variation in frame | Change your distance |
| **Skew** | Rotation / tilt angle diversity | Tilt the iPad at angles |

Calibration completes when the overall **diversity score** (weighted combination
of all four axes) reaches the threshold (default 0.35) AND at least 20 captures
have been collected. If diversity stagnates (no improvement in the last 5
captures), the system accepts what you have.

---

## Keyboard Controls (on the Mac)

| Key | Action |
|---|---|
| `q` | Quit immediately (no calibration) |
| `c` | Force-capture current frame (bypasses quality check) |
| `r` | Reset all samples and start over |

---

## Output Files

When calibration completes, three files are saved in the current directory:

| File | Format | Purpose |
|---|---|---|
| `calibration.json` | JSON | Human-readable intrinsics, distortion, quality |
| `calibration.npz` | NumPy | Programmatic use — `np.load("calibration.npz")` |
| `calibration_verification.jpg` | JPEG | Side-by-side original vs undistorted images |

### Loading results in code

```python
import numpy as np
import cv2

data = np.load("calibration.npz")
K    = data["camera_matrix"]      # 3x3 intrinsic matrix
dist = data["dist_coeffs"]        # 1x5 distortion coefficients
rms  = float(data["rms_error"])   # reprojection error (pixels)

# Undistort a frame
frame = cv2.imread("photo.jpg")
corrected = cv2.undistort(frame, K, dist)
```

---

## Quality Rating

| RMS Error | Rating |
|---|---|
| < 0.5 px | Excellent |
| 0.5 — 1.0 px | Good |
| > 1.0 px | Poor — consider recalibrating |

---

## Known iPad Models

Run `uv run python main.py --list_devices` to see all supported models:

```
  ipad-pro-11             iPad Pro 11" (1st / 2nd / 3rd / 4th gen)
  ipad-pro-12.9           iPad Pro 12.9" (3rd / 4th / 5th / 6th gen)
  ipad-pro-13             iPad Pro 13" M4 (2024)
  ipad-air-10.9           iPad Air 10.9" (4th / 5th gen)
  ipad-air-11             iPad Air 11" M2 (2024)
  ipad-10.2               iPad 10.2" (7th-9th gen)
  ipad-10.9               iPad 10.9" (10th gen, 2022)
```

If your device is not listed, measure the physical width of the displayed
pattern area on the iPad screen and pass it via `--board_width_mm`.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| iPad border stays red | Ensure the iPad screen is facing the camera and is within its field of view |
| RTSP stream timeout | Check the camera URL and network connectivity; retry the command |
| "No TTS engine found" | Install `espeak` (Linux) or use `--no_voice`; visual feedback still works |
| Quality bars stuck | Follow the voice guidance — change distance, tilt, or position |
| Captures not happening (auto mode) | Move more — the system waits for the board to enter a *new* quality region |
| Captures not happening (stable mode) | Hold the iPad still for ~1 second; reduce hand shake |
| Calibration completes too early | Increase `--diversity_threshold` (e.g., 0.50) or `--min_captures` |
| Calibration never completes | Decrease `--diversity_threshold` (e.g., 0.25) — or press `q` to quit with current samples |

---

## Platform Support

| Platform | Voice (TTS) | Screen Auto-Detect | iPad Mode |
|---|---|---|---|
| macOS | `say` (built-in) | Yes (Quartz) | Yes |
| Linux | `espeak` / `spd-say` / `festival` | No | Yes |
| Windows | PowerShell SAPI | No | Yes |

On all platforms, use `--board_width_mm` or `--ipad_model` for accurate
physical board dimensions.
