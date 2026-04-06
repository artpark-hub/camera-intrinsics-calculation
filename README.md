# MDCT-Proto — Mobile Dynamic Calibration Target

A Python-based tool that determines the intrinsic parameters of an RTSP camera using an iPad (or any tablet) as a mobile calibration target. It displays a ChArUco calibration pattern on the tablet, ingests the remote camera feed, detects the board, and **guides you through pose captures via voice and visual feedback** to compute camera intrinsics.

---

## Features

- **Two-Device Architecture** — iPad displays the pattern; laptop processes the feed. Real-time colored-border feedback on the iPad (red/amber/green) so you never have to look at the laptop.
- **Three Capture Modes** — `auto` (wave the iPad slowly, captures happen automatically), `timed` (fixed interval), or `stable` (hold still). Auto mode is the default and easiest.
- **Cross-Platform** — Runs on macOS, Linux, and Windows. TTS uses platform-native engines (macOS `say`, Linux `espeak`, Windows SAPI).
- **Smart Quality Tracking** — Diversity-based completion using entropy + coverage across X, Y, Size, and Skew axes. No more getting stuck chasing impossible bin fills.
- **Two-Pass Calibration** — With outlier rejection for robust results.
- **Voice Guidance** — Context-aware suggestions based on which quality axis is weakest.
- **RTSP / Video / Webcam** — Any input source supported.

---

## Quick Start

### 1. Setup

```bash
cd ~/Desktop/ART/3d/calib
chmod +x setup.sh
./setup.sh
```

This installs [uv](https://docs.astral.sh/uv/) (if needed), creates the virtual environment, and installs all dependencies.

### 2. Run (iPad mode — recommended)

```bash
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11
```

Then open `http://<your-laptop-ip>:8080/` on the iPad in Safari and tap to go fullscreen.

### 3. Calibrate

Just slowly wave the iPad around in front of the camera. The default **auto-capture** mode captures automatically when the board enters a new quality region — no need to hold still. Watch the iPad border:

| Border Color | Meaning |
|---|---|
| **Red, pulsing** | Camera can't see the board — keep moving into view |
| **Amber** | Board detected — keep moving slowly |
| **Green flash** | Captured! Move to a new position/angle/distance |

The system stops automatically when it has enough diverse samples (typically 20 captures, ~2-3 minutes).

---

## All Command-Line Options

### Input Source (one required)

| Argument | Description |
|---|---|
| `--rtsp URL` | RTSP stream URL |
| `--video PATH` | Path to a local video file |
| `--camera INDEX` | Webcam device index (e.g., 0) |

### iPad / Two-Device Mode

| Argument | Default | Description |
|---|---|---|
| `--ipad` | off | Serve the ChArUco pattern to an iPad/tablet via web |
| `--ipad_model MODEL` | — | Known device model for auto board-width (e.g., `ipad-pro-11`). Implies `--ipad`. |
| `--port PORT` | `8080` | Web server port |
| `--list_devices` | — | Print known iPad/tablet models and exit |

### Board Display

| Argument | Default | Description |
|---|---|---|
| `--board_width_mm N` | auto | Physical width of the displayed pattern (mm). Auto-detected from `--ipad_model` or Mac screen. |
| `--display_width N` | `1200` (iPad) / `900` (local) | Pixel width of the rendered ChArUco image |

### Capture Mode

| Argument | Default | Description |
|---|---|---|
| `--capture_mode MODE` | `auto` | Capture strategy: `auto` (movement-based, sharpest frame selection), `timed` (fixed interval), or `stable` (hold still) |
| `--capture_interval N` | `2.0` | Seconds between captures in `timed` mode |

### Calibration Parameters

| Argument | Default | Description |
|---|---|---|
| `--min_captures N` | `20` | Minimum pose captures before completion |
| `--bins N` | `5` | Number of histogram bins per quality axis |
| `--min_per_bin N` | `2` | Samples required per bin |
| `--outlier_rms N` | `0.5` | Per-frame RMS threshold for outlier rejection (px) |
| `--diversity_threshold N` | `0.35` | Diversity score (0-1) needed for completion. Lower = easier to finish. |

### Stability (used by `stable` capture mode)

| Argument | Default | Description |
|---|---|---|
| `--stability_frames N` | `8` | Frames to track for stability check |
| `--stability_threshold N` | `5.0` | Max corner variance (px) for stability |

### Output & Misc

| Argument | Default | Description |
|---|---|---|
| `--output NAME` | `calibration` | Output file base name |
| `--no_voice` | off | Disable voice guidance |
| `--no_board` | off | Don't display ChArUco board (use external pattern) |

---

## Usage Examples

```bash
# iPad auto-capture (recommended — just wave the iPad slowly)
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11

# Timed capture — one capture every 3 seconds
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11 \
    --capture_mode timed --capture_interval 3

# Legacy stable mode (requires holding iPad still)
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --ipad_model ipad-pro-11 \
    --capture_mode stable

# Local-screen mode (pattern on laptop, no iPad)
uv run python main.py --rtsp rtsp://192.168.0.64:5543/live/channel0 \
    --board_width_mm 280

# Webcam instead of RTSP
uv run python main.py --camera 0 --ipad --ipad_model ipad-pro-11

# List known iPad models
uv run python main.py --list_devices
```

---

## Keyboard Controls

| Key | Action |
|---|---|
| `q` | Quit immediately |
| `c` | Force-capture current frame (bypasses quality check) |
| `r` | Reset all samples and start over |

---

## Output Files

| File | Format | Purpose |
|---|---|---|
| `calibration.json` | JSON | Human-readable intrinsics, distortion, quality |
| `calibration.npz` | NumPy | Programmatic use |
| `calibration_verification.jpg` | JPEG | Side-by-side original vs undistorted |

```python
import numpy as np
data = np.load("calibration.npz")
K    = data["camera_matrix"]      # 3x3 intrinsic matrix
dist = data["dist_coeffs"]        # 1x5 distortion coefficients
rms  = float(data["rms_error"])   # reprojection error (pixels)
```

---

## Architecture

```
main.py               — Entry point, CLI, main capture loop
pattern_display.py     — ChArUco board generation + rendering
feed_ingestion.py      — Threaded RTSP/video/webcam reader
detector.py            — ChArUco detection, pose estimation, sharpness scoring
quality_tracker.py     — Diversity-based quality tracking (4-axis histograms)
guidance.py            — Cross-platform voice guidance state machine
calibrator.py          — Two-pass calibration with outlier rejection
overlay.py             — HUD drawing (quality bars, axes, status)
exporter.py            — JSON/NPZ/verification image export
web_server.py          — Flask server for iPad mode (SSE + MJPEG)
screen_measure.py      — Screen dimension detection + iPad model database
```

---

## Documentation

| Document | Description |
|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | Step-by-step usage instructions |
| [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) | How it works — architecture, math, algorithms |
| [design_doc.md](design_doc.md) | Original design specification |

---

## Quality Benchmarks

| RMS Error | Rating |
|---|---|
| < 0.5 px | Excellent (suitable for 3D reconstruction) |
| 0.5 — 1.0 px | Good (suitable for most applications) |
| > 1.0 px | Poor (consider recalibrating) |

---

## Platform Support

| Platform | Voice (TTS) | Screen Auto-Detect | iPad Mode |
|---|---|---|---|
| macOS | `say` (built-in) | Yes (Quartz) | Yes |
| Linux | `espeak` / `spd-say` / `festival` | No | Yes |
| Windows | PowerShell SAPI | No | Yes |
