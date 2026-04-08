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
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133
```

Then open `http://<your-laptop-ip>:8080/` on the iPad in Safari and tap to go fullscreen. The `--board_width_mm` value should be the physical width of the displayed ChArUco pattern measured with a ruler — corner of the outermost square to corner of the opposite outermost square. (See [A note on board width and intrinsics](#a-note-on-board-width-and-intrinsics) for what this value affects and what it doesn't.)

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
| `--source URL` | Any FFmpeg-readable stream: `rtsp://…`, HLS `http(s)://…/index.m3u8`, HTTP-MJPEG, or a MediaMTX republish such as `rtsp://mediamtx-host:8554/ART-65`. `--rtsp` is kept as a backwards-compatible alias. |
| `--proc_height N` | Optional internal downscale (height in pixels). **Leave at 0** (default) for accurate calibration — any positive value means the resulting intrinsics describe the resized frame, not the camera. Only use if CPU-bound. |
| `--video PATH` | Path to a local video file |
| `--camera INDEX` | Webcam device index (e.g., 0) |

### iPad / Two-Device Mode

| Argument | Default | Description |
|---|---|---|
| `--ipad` | off | Serve the ChArUco pattern to an iPad/tablet via web. Open `http://<your-ip>:8080/` on the iPad in Safari. |
| `--port PORT` | `8080` | Web server port |

### Board Display

| Argument | Default | Description |
|---|---|---|
| `--board_width_mm N` | auto-detect on Mac, else required for iPad | Physical width (mm) of the displayed ChArUco pattern, measured with a ruler from corner-to-corner of the outermost squares. **Only affects tvec scale, not the intrinsic matrix or distortion.** |
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
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133

# Timed capture — one capture every 3 seconds
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133 \
    --capture_mode timed --capture_interval 3

# Legacy stable mode (requires holding iPad still)
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0 \
    --ipad --board_width_mm 133 \
    --capture_mode stable

# Local-screen mode (pattern on laptop, no iPad — board width auto-detected on macOS)
uv run python main.py --source rtsp://192.168.0.64:5543/live/channel0

# Webcam instead of RTSP
uv run python main.py --camera 0 --ipad --board_width_mm 133
```

---

## Stream Sources

`--source` accepts anything FFmpeg can open. The flag was historically called `--rtsp` and that name still works as an alias, but the tool is *not* limited to RTSP.

| Source type | Example | When to use |
|---|---|---|
| **Direct RTSP** | `rtsp://admin:pass@192.168.1.50:554/cam/realmonitor?channel=1&subtype=0` | You're on the same L2/L3 segment as the camera and credentials are known. Lowest latency. |
| **MediaMTX (or any RTSP proxy) republish** | `rtsp://mediamtx-host:8554/ART-65` | The camera is on a different subnet / behind an ACL, but a media server in your infrastructure already pulls it. No credentials needed in your command line. **Recommended for multi-camera deployments.** |
| **HLS playlist** | `http://mediamtx-host:8888/ART-65/index.m3u8` | Firewalls only allow HTTP/HTTPS. Adds 1-3 s of latency, which can hurt auto-capture mode — prefer the RTSP republish above when you have the choice. |
| **HTTP-MJPEG** | `http://camera/mjpeg` | Older IP cameras and webcam servers. Works fine for calibration. |
| **Local video file** | use `--video path/to/file.mp4` | Offline testing / dataset replay. |
| **Webcam** | use `--camera 0` | Built-in or USB webcam. |

### Quoting tip

If your URL contains `?`, `&`, or `@` (most RTSP URLs do), wrap it in **single quotes** so your shell doesn't interpret those characters:

```bash
uv run python main.py \
  --source 'rtsp://admin:Pass%402025@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0' \
  --ipad --board_width_mm 133
```

In zsh, an unquoted `?` triggers `no matches found` (glob expansion) and an unquoted `&` puts the command in the background. Single quotes fix both.

### Diagnosing connection problems

If the stream times out or refuses, run these to localise the fault before touching the calib tool:

```bash
nc -vz <camera-ip> 554                                  # is port 554 even open?
ffprobe -rtsp_transport tcp '<full rtsp url>'           # what does the camera actually say?
```

Common outcomes:
- **`Connection refused`** — port not open from your host. Either the camera's RTSP service is off, the camera has an IP whitelist, or a firewall/ACL between you and the camera is rejecting. Use a MediaMTX republish that lives on a host with permission.
- **Timeout (no reply)** — packets are being silently dropped. Forcing TCP transport sometimes helps: `OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp" uv run python main.py …`
- **`401 Unauthorized`** — credentials are wrong or not URL-encoded (e.g. `@` in the password must become `%40`).
- **`404 Not Found` / `Stream not found`** — wrong path. Different brands use very different paths (Hikvision: `/Streaming/Channels/101`, Dahua: `/cam/realmonitor?channel=1&subtype=0`, Reolink: `/h264Preview_01_main`, …).

---

## Keyboard Controls

| Key | Action |
|---|---|
| `q` | Quit immediately |
| `c` | Force-capture current frame (bypasses quality check) |
| `r` | Reset all samples and start over |

---

## Getting Accurate Results

Camera intrinsic calibration is **physics-limited**. Whether the resulting numbers are correct depends almost entirely on what you showed the camera, not on the solver. The two failure modes that produce a "looks great, RMS 0.07" result that is silently wrong:

1. **The pattern never reaches the corners of the image.** Lens distortion is zero at the principal point and grows with radial distance from it. If your iPad only ever appears in the inner ~60% of the frame, the solver has *no* observations at large radii — `(k1, k2, k3)` get fit to noise.
2. **The frame was silently downscaled.** Intrinsics live in pixel units. A `K` matrix calibrated at 1280×720 cannot be applied to a 2560×1440 stream without scaling, and sub-pixel corner localization on the smaller frame is degraded.

### Pre-flight checklist

- ✅ **Use the camera's native resolution.** Do not pass `--proc_height`. Confirm the startup banner says `Processing at native resolution`.
- ✅ **Use the largest pattern you have.** iPad Pro 12.9" beats iPad Pro 11" beats iPad mini.
- ✅ **Get close.** For an iPad Pro 11" on a 65° FOV camera, that's roughly **40-60 cm from the lens**, much closer than people instinctively hold it.
- ✅ **Push the iPad to all four corners and edges of the camera image** — not the centre. The corners are where distortion lives.
- ✅ **Tilt aggressively** at every position so the Skew bar fills.
- ✅ **Read the health-check block** the calibrator prints at the end. If it warns about `|k1| > 1.5`, `|k3| > 1`, `RMS < 0.10`, `edge-gap > 15%`, or `fx ≠ fy`, the calibration is wrong regardless of how low the RMS looks. Recapture.

### A note on board width and intrinsics

A common worry: *"what if my `--board_width_mm` (or auto-detected iPad width) is wrong by 10%? Are my fx/fy off by 10%?"*

**No.** Camera intrinsic calibration via Zhang's method is **scale-invariant in the object points**. If you scale every 3D board coordinate by a factor α, the homography between the board plane and the image plane absorbs that factor entirely into the camera's translation vector — the resulting `K` matrix and distortion coefficients are unchanged. Concretely: doubling the assumed board width doubles every reported `tvec`, but `(fx, fy, cx, cy, k1, k2, …)` come out identical to the bit.

What `board_width_mm` *does* affect:

- The absolute scale of `tvecs` returned by the calibrator and any downstream PnP pose distance you compute. So **if you care about real-world distances**, get the value right (measure the rendered pattern with a ruler).
- Nothing else. The intrinsics that go into `K` and `dist_coeffs` are determined entirely by image-plane geometry.

This is why the iPad workflow can produce a sensible `K` even when `--board_width_mm` is mismeasured — and also why it can't rescue distortion from a small target in the centre of the frame (that's a corner-coverage problem, not a scale problem).

### Defaults that protect you

The calibrator now applies two constraints by default:
- `CALIB_FIX_K3` — k3 is held at 0 (it's the first parameter to go wild without corner coverage).
- `CALIB_FIX_ASPECT_RATIO` — fx is forced to equal fy (square-pixel sensors require this).

Override only if you have a non-square-pixel sensor or full image-corner coverage.

### What "good" looks like

| Quantity | Healthy range |
|---|---|
| fx, fy | within 0.5% of each other |
| cx, cy | within ~5% of image centre |
| \|k1\| | 0.1 – 0.6 |
| \|k2\| | < 0.5 |
| k3 | 0 (held fixed) |
| \|p1\|, \|p2\| | < 0.005 |
| RMS | **0.20 – 0.50 px** (below 0.10 = overfitting) |
| Edge-gap | < 10% |

### Two-stage workflow (recommended for production)

A small mobile target genuinely cannot constrain distortion well on a high-resolution sensor. The honest answer is to split the problem:

- **Stage 1 — Distortion (one-time, by an installer):** calibrate once with a printed **A2/A1 chessboard** brought right up to the lens so it fills the frame and tilts into every corner. Save `(k1, k2, k3, p1, p2)` as a per-camera factory file.
- **Stage 2 — Refresh focal/principal (in the field, with the iPad):** use the iPad workflow only to refresh `(fx, fy, cx, cy)` after focus drift or thermal cycling, with distortion held fixed at the factory values.

See `USER_GUIDE.md` for the full version of this section.

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
screen_measure.py      — macOS screen auto-detection (Quartz) for local-screen mode
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
