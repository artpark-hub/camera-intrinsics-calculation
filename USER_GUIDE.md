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
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133
```

**Timed-capture mode:**

```bash
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133 \
    --capture_mode timed --capture_interval 3
```

**Stable mode (legacy — requires holding iPad still):**

```bash
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133 \
    --capture_mode stable
```

**Via a MediaMTX republish (when the camera is on a different subnet or behind an ACL):**

```bash
uv run python main.py --source rtsp://mediamtx-host:8554/ART-65 \
    --ipad --board_width_mm 133
```

You don't need the camera credentials in this case — MediaMTX (or any other RTSP proxy in your infrastructure) handles the upstream pull. This is the recommended approach for multi-camera deployments.

> **Quoting tip:** if your URL contains `?`, `&`, or `@` (most direct RTSP URLs do), wrap it in **single quotes**. In zsh, an unquoted `?` triggers `no matches found` and an unquoted `&` puts the command in the background. Single quotes fix both.

### Common CLI options

| Flag | Purpose | Default |
|---|---|---|
| `--source URL` | Stream URL — RTSP, HLS (`.m3u8`), HTTP-MJPEG, or a MediaMTX republish. `--rtsp` is accepted as an alias. | — |
| `--ipad` | Enable two-device iPad mode (serves the pattern over HTTP at port `--port`) | off |
| `--capture_mode MODE` | `auto`, `timed`, or `stable` | `auto` |
| `--capture_interval N` | Seconds between captures in timed mode | `2.0` |
| `--board_width_mm N` | Physical width (mm) of the displayed pattern, measured with a ruler from corner-to-corner of the outermost squares. Required for iPad mode. Auto-detected on macOS for local-screen mode. | auto / required |
| `--min_captures N` | Minimum poses to collect | `20` |
| `--diversity_threshold N` | Diversity score needed to complete (0-1) | `0.35` |
| `--port N` | Web server port | `8080` |
| `--no_voice` | Disable voice guidance | off |
| `--proc_height N` | Optional internal downscale (height in pixels). **Leave at 0** for accurate calibration — any positive value means the resulting intrinsics describe the resized frame, not the camera, AND sub-pixel corner localization is degraded. Only use if CPU-bound. | `0` (native) |

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

## Getting Accurate Results — Read This First

Camera intrinsic calibration is **physics-limited**, not software-limited.
Whether the resulting numbers are correct depends almost entirely on what you
showed the camera, not on the solver. The two failure modes that produce
"looks great, RMS 0.07" results that are silently wrong are:

1. **The pattern never reaches the corners of the image.** Lens distortion
   `(k1, k2, k3)` is, by definition, zero at the principal point and grows
   with the radial distance from it. If your iPad only ever appears in the
   inner ~60% of the frame, the solver has no observations at large radii
   and is free to invent any distortion model that fits the noise in the
   centre. The result is a low RMS but a totally wrong distortion estimate.

2. **The pattern is too small relative to the sensor.** A 16 cm wide iPad in
   front of a 2560 × 1440 camera at 2 m only covers a small fraction of the
   frame. You can't get the *corners of the iPad* to the *corners of the
   image* without either getting close, or using a much larger pattern.

The fixes are mechanical, not algorithmic:

### Pre-flight checklist

| ✓ | Check |
|---|---|
| ☐ | **Use the camera's native resolution.** Do not pass `--proc_height`. The intrinsics MUST describe the resolution you'll actually use the camera at. If your camera delivers 2560×1440, calibrate at 2560×1440. The startup banner now prints "Source frame size" and "Processing at native resolution" — confirm both. |
| ☐ | **Use the largest pattern you have.** iPad Pro 12.9" > iPad Pro 11" > iPad mini. Better still, print an A2 ChArUco board for the very first calibration of a new camera (see two-stage workflow below). |
| ☐ | **Get close enough that the iPad fills at least half the frame.** For an iPad Pro 11" (≈16 cm wide) on a 65° FOV camera, that's roughly **40-60 cm from the lens** — much closer than people instinctively hold it. |
| ☐ | **Push the pattern to all four corners and edges of the image.** Not the centre. The corners. The HUD now reports an "edge-gap" health metric — aim for it to be under 10%. |
| ☐ | **Tilt aggressively.** Lean the iPad forward, back, left, right at every position. Without tilt, focal length is degenerate with object-distance. |
| ☐ | **Read the health check at the end.** The calibrator now prints a `CALIBRATION HEALTH CHECK` block with warnings. If you see any of: `|k1| > 1.5`, `|k3| > 1`, `RMS < 0.10`, `edge-gap > 15%`, or `fx ≠ fy by > 1%` — the calibration is wrong, regardless of how low the RMS looks. Recapture. |

### What does (and does not) depend on `board_width_mm`

A common worry: *"what if my iPad's board width is wrong by 10% — are my fx/fy off by 10%?"*

**No.** Camera intrinsic calibration via Zhang's method is *scale-invariant in
the object points*. If you scale every 3D board coordinate by a factor α, the
homography between the board plane and the image plane absorbs that factor
entirely into the camera's translation vector. The resulting **K matrix and
distortion coefficients are unchanged**.

What `board_width_mm` *does* affect:

- The absolute scale of `tvecs` returned by the calibrator and any downstream
  PnP distance you derive from it.
- Nothing else. `(fx, fy, cx, cy, k1, k2, k3, p1, p2)` are determined entirely
  by image-plane geometry — not by what you told the solver about how big the
  board was in millimetres.

So **if you only care about intrinsics**, an inaccurate `--board_width_mm` is
harmless. If you care about real-world pose distances (e.g., "how far is that
person from the camera?"), measure the rendered pattern on the iPad with a
ruler (corner of outermost square to corner of outermost square) and pass the
measured value via `--board_width_mm`.

There used to be an `--ipad_model` flag with a database of known iPad
viewports for auto-computing this value, but it was removed because a ruler
is more reliable than any database — fullscreen state, orientation, Display
Zoom, Split-View, and the URL bar all change the effective viewport, and
none of them are reliably detectable from outside Safari. Just measure.

### Two-stage workflow (recommended for production)

A small mobile target genuinely cannot constrain distortion well on a
high-resolution sensor. The honest answer is to split the problem:

**Stage 1 — Distortion (one-time, by an installer):**
Calibrate the camera once with a printed **A2 or A1 chessboard / ChArUco
board** brought right up to the lens so it fills the frame and tilts into
every corner. Save the resulting `(k1, k2, k3, p1, p2)` as a per-camera
factory file. This is the calibration the chess-board reference you compared
against was doing.

**Stage 2 — Refresh focal/principal (in the field, with the iPad):**
Use the iPad workflow only to refresh `(fx, fy, cx, cy)` after focus drift
or thermal cycling, with the distortion coefficients held fixed at their
factory values. This works because focal length and principal point can be
estimated from a small central target — only distortion needs the corners.

Support for fixing distortion to factory values is on the roadmap; for now,
if you need accurate distortion you must do Stage 1 with a large rigid
target.

### What "good" looks like

A healthy calibration on a typical 1080p/1440p IP camera should show:

- **fx ≈ fy** within 0.5%
- **cx, cy** within ~5% of the image centre
- **|k1|** roughly 0.1 – 0.6 (mild barrel for wide lenses, mild pincushion for tele)
- **|k2|** under 0.5
- **k3 ≡ 0** (held fixed by default)
- **|p1|, |p2|** under 0.005
- **RMS** between **0.20 and 0.50 px**. Below 0.10 px is a red flag for overfitting.
- **Edge-gap** under 10% (corners reach within 10% of every image edge).

---

## The Ideal Routine (Auto Mode)

Think of it as **a slow sweep covering positions, distances, angles, AND
the four corners of the camera image**:

1. **Start close** (~40-60 cm) — sweep the iPad to **each of the four corners**
   of the camera view. Hold it at the corner long enough for one capture, then
   move to the next corner. This is the single most important phase — it
   gives the solver its only observations at large radial distances.
2. **Step back** (~1.5 m) — sweep left-to-right across the frame, varying
   height (hold high, then low).
3. **Step back more** (~3 m) — sweep again.
4. **Throughout** — tilt the iPad: forward, back, left, right. The "Skew"
   bar should always be filling.

The system captures automatically as you cover new regions. The voice
guidance tells you what's still needed. A typical calibration takes
**2-3 minutes** and **20-30 captures**.

When the run finishes, **read the health check block**. If it prints
warnings, recapture rather than trusting the numbers.

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

## Measuring `--board_width_mm`

The application no longer keeps a database of iPad models — that approach
needed to know the exact CSS viewport, fullscreen state, orientation,
Split-View status, and zoom level of every device, and got most of them
subtly wrong. **A ruler is more reliable than any model database.**

Once the iPad is showing the ChArUco pattern (in fullscreen):

1. Hold a ruler (mm side) against the iPad screen.
2. Measure from the **corner of the outermost square** on one side to the
   **corner of the outermost square** on the opposite side, along the
   **wider** edge of the rendered board (i.e., 5 squares wide for the
   default 5×7 board).
3. Pass that value as `--board_width_mm`.

That measurement only affects the absolute scale of returned `tvecs` —
Zhang's method is scale-invariant in object points, so `(fx, fy, cx, cy,
k1, k2, k3, p1, p2)` are determined entirely by image-plane geometry. If
you only care about intrinsics you can skip the measurement; the
calibration will still produce a correct `K` matrix.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| iPad border stays red | Ensure the iPad screen is facing the camera and is within its field of view |
| RTSP stream timeout | Check the camera URL and network connectivity; retry the command |
| RTSP "Connection refused" but the camera works in another app | You're probably on a different subnet than the camera and port 554 is blocked by a firewall/ACL. Point `--source` at a MediaMTX (or other media-server) republish instead — e.g. `--source rtsp://<mediamtx-host>:8554/<camera-name>`. HLS URLs (`http(s)://…/index.m3u8`) also work but add 1-3 s of latency which can hurt auto-capture. |
| "No TTS engine found" | Install `espeak` (Linux) or use `--no_voice`; visual feedback still works |
| Quality bars stuck | Follow the voice guidance — change distance, tilt, or position |
| Captures not happening (auto mode) | Move more — the system waits for the board to enter a *new* quality region |
| Captures not happening (stable mode) | Hold the iPad still for ~1 second; reduce hand shake |
| Calibration completes too early | Increase `--diversity_threshold` (e.g., 0.50) or `--min_captures` |
| Calibration never completes | Decrease `--diversity_threshold` (e.g., 0.25) — or press `q` to quit with current samples |
| Health check warns "RMS suspiciously LOW" | Overfitting. The captures cluster in a small region of the frame. Recapture with the iPad pushed to **all four image corners** at close range. |
| Health check warns "edge-gap > 15%" | Your captures never reach the image edges, so distortion (k1, k2) is unconstrained. Get the iPad closer and physically sweep it through each of the four corners of the camera view. |
| Health check warns "\|k1\| > 1.5" or "\|k3\| > 1" | The distortion model is fitting noise. Same fix as above — push to image corners. k3 is fixed at 0 by default; if you see it nonzero you've passed `fix_k3=False`. |
| Health check warns "fx ≠ fy by > 1%" | Underconstrained solve. fix_aspect_ratio is on by default; if you see this you've turned it off. Re-enable it. |
| Intrinsics from this run don't match a previous chessboard calibration | First check: was the previous calibration done at the same resolution? Run `--proc_height 0` (default) and confirm the startup banner says "Source frame size : 2560x1440 / Processing at native resolution". A 1280×720 calibration is **not** comparable to a 2560×1440 calibration without scaling. |

---

## Platform Support

| Platform | Voice (TTS) | Screen Auto-Detect | iPad Mode |
|---|---|---|---|
| macOS | `say` (built-in) | Yes (Quartz) | Yes |
| Linux | `espeak` / `spd-say` / `festival` | No | Yes |
| Windows | PowerShell SAPI | No | Yes |

On all platforms, use `--board_width_mm <measured>` for accurate physical
board dimensions in iPad mode.
