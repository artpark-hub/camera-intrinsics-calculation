# MDCT-Proto (Mobile Dynamic Calibration Target): RTSP Camera Intrinsic Calibration

## Technical Overview

This document explains how the MDCT-Proto calibration system works.
It is written for engineers who may not have a computer vision background.

---

## Table of Contents

1. What is Camera Calibration?
2. The ChArUco Board
3. Two-Device Architecture
4. Capture Pipeline
5. Three Capture Modes
6. Quality Tracking
7. Diversity Score and Completion
8. The Math: How Intrinsics Are Computed
9. Two-Pass Calibration with Outlier Rejection
10. Output
11. Cross-Platform Support

---

## 1. What is Camera Calibration?

Every camera lens introduces small distortions. A straight line in the
real world may appear slightly curved in the image. Calibration figures
out the mathematical model of your specific camera so you can undo
those distortions.

Think of it like fitting glasses. The optometrist measures how your
eye bends light (your "prescription"), then grinds lenses to correct
it. Calibration measures how *your camera* bends light, and produces
a correction recipe.

### What We Solve For

**Intrinsic parameters** describe the internal geometry of the camera:

```
                Intrinsic Matrix (K)
                --------------------
                | fx   0   cx |
            K = |  0  fy   cy |
                |  0   0    1 |

    fx, fy  =  focal lengths (in pixels)
                How much the lens "zooms" in X and Y.

    cx, cy  =  principal point
                Where the optical axis hits the sensor.
                Ideally the image centre, but usually off by a few pixels.
```

**Distortion coefficients** model how the lens warps the image:

```
    k1, k2, k3   =  radial distortion   (barrel / pincushion)
    p1, p2        =  tangential distortion (slight lens tilt)
```

### Barrel vs Pincushion Distortion (ASCII)

```
    Barrel distortion         No distortion        Pincushion distortion
   (k1 < 0, typical           (perfect lens)         (k1 > 0)
    for wide-angle)

    . -- ~ ~ -- .            +---+---+---+          .         .
  /   /   |   \   \          |   |   |   |           \   |   /
 |  /  +--+--+  \  |        +---+---+---+            |  +--+--+  |
 | |   |  |  |   | |        |   |   |   |            | |   |  |   | |
 |  \  +--+--+  /  |        +---+---+---+            |  +--+--+  |
  \   \   |   /   /          |   |   |   |           /   |   \
    ' -- _ _ -- '            +---+---+---+          '         '

  Straight lines bow         Straight lines          Straight lines
  outward (bulge)            stay straight            pinch inward
```

Most real cameras have slight barrel distortion. The calibration
measures exactly how much, so software can straighten every frame.

---

## 2. The ChArUco Board

A ChArUco board combines a **ch**eckerboard with **ArUco** markers:

```
    ChArUco Board (5 cols x 7 rows)
    ================================

    +-------+-------+-------+-------+-------+
    |       | @@@@@ |       | @@@@@ |       |
    | white | @ArU@ | white | @ArU@ | white |
    |       | @co @ |       | @co @ |       |
    +-------+-------+-------+-------+-------+
    | @@@@@ |       | @@@@@ |       | @@@@@ |
    | @ArU@ | white | @ArU@ | white | @ArU@ |
    | @co @ |       | @co @ |       | @co @ |
    +-------+-------+-------+-------+-------+
    |       | @@@@@ |       | @@@@@ |       |
    |  ...  | @  @ |  ...  | @  @ |  ...  |
    :       :       :       :       :       :
    (continues for 7 rows)

    Each @@@ block is a unique ArUco marker (from DICT_4X4_50).
    The corners where black/white squares meet are the calibration
    points (up to 24 interior corners for a 5x7 board).
```

### Why ChArUco instead of a plain checkerboard?

A plain checkerboard requires **all** corners to be visible. If your
finger covers one corner, or the board is partially off-screen, the
entire detection fails.

ChArUco boards embed uniquely identifiable ArUco markers in the black
squares. Each marker has a known ID, so the system can identify
individual corners even when only part of the board is visible:

```
    Plain checkerboard:               ChArUco board:
    ALL corners needed                PARTIAL visibility OK

    +---+---+---+---+                 +---+---+---+---+
    |   |   |   |   |   <-- if any   |   |ID3|   |ID7|
    +---+---+---X---+   corner X     +---+---+---+---+
    |   |   |   |   |   is hidden    |ID2|   |ID5|   |   <-- only top
    +---+---+---+---+   = FAIL       +---+---+---+---+       half visible
    |   |   |   |   |                                         = still works!
    +---+---+---+---+                 (markers identify
    |   |   |   |   |                  which corners are
    +---+---+---+---+                  which)
```

This makes ChArUco far more practical for handheld calibration where
the board will inevitably go partially out of frame.

---

## 3. Two-Device Architecture

The recommended setup uses two devices on the same network:

```
    ┌─────────────────┐      RTSP stream       ┌──────────────┐
    │   RTSP Camera   │ ─────────────────────>  │    Laptop    │
    │  (IP camera or  │   (H.264 video over     │  (main.py)   │
    │   phone app)    │    TCP/UDP)              │              │
    └─────────────────┘                          │  - detect    │
                                                 │  - track     │
                                                 │  - calibrate │
                                                 └──────┬───────┘
                                                        │
                                                        │  Flask web server
                                                        │  (port 8080)
                                                        │
                                          ┌─────────────┴──────────────┐
                                          │                            │
                                    ┌─────▼─────┐              ┌──────▼──────┐
                                    │   iPad     │              │  /monitor   │
                                    │            │              │  (optional  │
                                    │ Shows the  │              │   MJPEG     │
                                    │ ChArUco    │              │   viewer)   │
                                    │ pattern    │              └─────────────┘
                                    │ + colored  │
                                    │ border     │
                                    └────────────┘
```

### Data flow

1. The iPad loads `http://<laptop-ip>:8080/` in Safari and displays
   the ChArUco pattern fullscreen.

2. The user holds the iPad in front of the RTSP camera.

3. The laptop grabs frames from the RTSP stream (via a background
   thread to avoid blocking on network I/O).

4. The laptop detects the board, evaluates quality, and pushes state
   back to the iPad via **Server-Sent Events (SSE)**:

```
    Laptop (main.py)                 iPad (browser)
    ================                 ==============

    detect board in frame
            |
            v
    evaluate quality metrics
            |
            v
    update_detection_state()  ──SSE──>  JavaScript setState()
      "no_detection"                      border = RED (pulsing)
      "detected"                          border = AMBER
      "stable"                            border = GREEN (glow)
      "captured"                          border = GREEN (flash)
      "complete"                          border = GREEN (solid)
```

### Colored border feedback

The iPad's screen border changes color so the person holding it
knows what the camera sees -- without turning around to look at
the laptop:

```
    ┌──────────────────────────────┐
    │ RED BORDER (pulsing)         │    Camera cannot see the board.
    │  ┌────────────────────────┐  │    Keep moving into view.
    │  │                        │  │
    │  │      ChArUco           │  │
    │  │      pattern           │  │
    │  │                        │  │
    │  └────────────────────────┘  │
    └──────────────────────────────┘

    ┌──────────────────────────────┐
    │ AMBER BORDER                 │    Board detected! Hold position
    │  ┌────────────────────────┐  │    and stay still.
    │  │                        │  │
    │  │      ChArUco           │  │
    │  │      pattern           │  │
    │  │                        │  │
    │  └────────────────────────┘  │
    └──────────────────────────────┘

    ┌──────────────────────────────┐
    │ GREEN BORDER (flash)         │    Captured! Move to a new
    │  ┌────────────────────────┐  │    position / angle.
    │  │                        │  │
    │  │      ChArUco           │  │
    │  │      pattern           │  │
    │  │                        │  │
    │  └────────────────────────┘  │
    └──────────────────────────────┘
```

A status bar at the bottom of the iPad screen shows the sample
count and current instruction text.

---

## 4. Capture Pipeline

Each frame goes through this pipeline:

```
    ┌──────────────┐
    │  RTSP stream  │
    │  (threaded    │
    │   capture)    │
    └──────┬───────┘
           │  latest frame
           v
    ┌──────────────┐
    │  Resize to   │   Downscale to 720p for faster processing.
    │  720p        │   Original aspect ratio preserved.
    └──────┬───────┘
           │
           v
    ┌──────────────┐
    │  ArUco        │   Detect ArUco markers in the frame.
    │  Detection    │   Uses adaptive thresholding + sub-pixel
    └──────┬───────┘   corner refinement.
           │
           │  markers found?
           │
      NO   │   YES
    ┌──┐   │   ┌──────────────┐
    │  │<──┘──>│  ChArUco     │   Interpolate checkerboard corners
    │  │       │  Interpolation│   from the detected markers.
    │  │       └──────┬───────┘
    │  │              │
    │  │              │  >= 6 corners?
    │  │              │
    │  │         YES  v
    │  │       ┌──────────────┐
    │  │       │  Compute     │   Four metrics: X, Y, Size, Skew.
    │  │       │  Quality     │   (See Section 6.)
    │  │       │  Metrics     │
    │  │       └──────┬───────┘
    │  │              │
    │  │              v
    │  │       ┌──────────────┐
    │  │       │  Capture     │   Depends on --capture_mode:
    │  │       │  Decision    │   auto:   new quality region?
    │  │       │              │   timed:  interval elapsed?
    │  │       │              │   stable: board held still?
    │  │       └──────┬───────┘
    │  │              │
    │  │              │  ready to capture?
    │  │              │
    │  │         YES  v
    │  │       ┌──────────────┐
    │  │       │  CAPTURE     │   Store corners + IDs.
    │  │       │              │   Update quality histogram.
    │  │       │              │   Push "captured" to iPad.
    │  │       └──────┬───────┘
    │  │              │
    │  │              v
    │  │       ┌──────────────┐
    │  │       │  Good enough?│   Check diversity score +
    │  │       │              │   min sample count.
    │  │       └──────┬───────┘
    │  │              │
    │  │         YES  v
    │  │       ┌──────────────┐
    │  │       │  STOP LOOP   │   Proceed to calibration.
    │  │       └──────────────┘
    │  │
    │  │  (no detection or not ready)
    │  │
    │  v
    │  Push state to iPad (red/amber)
    │  Draw HUD overlay on feed window
    │  Loop back for next frame
    └─────────────────────────────────>
```

### Threaded RTSP Capture

RTSP streams have network latency. If the main loop blocked on each
`read()`, it would get stale frames and lag behind. The
`ThreadedCapture` class runs a background thread that continuously
grabs the latest frame, so the main loop always processes the most
recent image:

```
    Background thread              Main thread
    =================              ===========

    while running:                 while running:
      ret, frame = cap.read()        ret, frame = tcap.read()
      lock.acquire()                   ^
      self.frame = frame               |
      lock.release()                   +-- returns a copy of
                                           whatever the background
      (blocks on network I/O,             thread last grabbed
       ~30-100ms per frame)
```

---

## 5. Three Capture Modes

The system supports three strategies for deciding when to save a frame.
Use `--capture_mode` to select: `auto` (default), `timed`, or `stable`.

### Auto Mode (`--capture_mode auto`, default)

The most user-friendly mode. Just slowly wave the iPad around --
captures happen automatically when the board enters a new quality
region.

Keeps a rolling buffer of the last ~15 frames with their detections.
When the quality metrics land in a previously under-represented
histogram bin, the system picks the **sharpest** frame from the
buffer and captures it.

```
    User slowly waves iPad in front of camera
    ------------------------------------------

    Frame 1:  board at (x=0.3, size=0.4)   --> buffer it
    Frame 2:  board at (x=0.3, size=0.4)   --> buffer it
    Frame 3:  board at (x=0.3, size=0.4)   --> buffer it  (same region)
    ...
    Frame 10: board at (x=0.7, size=0.2)   --> NEW region detected!
              Pick sharpest frame from buffer --> CAPTURE
              Clear buffer, start fresh
```

Sharpness is measured by the **Laplacian variance** of the board's
bounding-box region. Higher variance = sharper edges = less motion
blur. This means you never need to hold still -- the system
automatically picks the least-blurry moment from your movement.

After each capture, there is a 0.8-second cooldown before the next.

### Timed Mode (`--capture_mode timed`)

Captures every N seconds (`--capture_interval`, default 2s) whenever
the board is visible. The simplest mode -- no stability requirement.
The user just needs to keep the board in view. Useful when you want
a fixed-rate capture regardless of movement.

### Stable Mode (`--capture_mode stable`, legacy)

The board must be held still for several frames before capture.
This was the original mode and requires the most effort from the user.

```
    Frame 1: board moving      --> corner variance HIGH  --> wait
    Frame 2: board moving      --> corner variance HIGH  --> wait
    Frame 3: board slowing     --> variance decreasing   --> wait
    ...
    Frame 8: board still       --> variance < threshold  --> CAPTURE
```

The stability check computes the variance of the mean corner position
across the last N frames (default 8). If the variance is below
5.0 px, the board is considered stable. 1.5-second cooldown between
captures.

---

## 6. Quality Tracking

Good calibration requires *diverse* samples. If you only hold the
board in the centre of the frame at the same distance, you get a
poor calibration even with 100 captures.

The quality tracker maintains four histograms, one for each axis
of variation:

```
    Axis     What it measures                        Range
    ----     -------------------                     -----
    X        Horizontal position of board centre     [0, 1]
             (0 = left edge, 1 = right edge)

    Y        Vertical position of board centre       [0, 1]
             (0 = top, 1 = bottom)

    Size     Board area as fraction of frame area    [0, 1]
             (sqrt-scaled so small boards spread
              more evenly across bins)

    Skew     Perspective distortion from tilting      [0, 1]
             (0 = flat, 1 = extreme tilt)
```

Each axis is divided into bins (default: 5). Every captured sample
increments the bin it falls into.

### Bin Ranges

Each axis maps its [0, 1] range into 5 equal-width bins:

```
    Bin        0        1        2        3        4
    Range   [0.0,    [0.2,    [0.4,    [0.6,    [0.8,
             0.2)     0.4)     0.6)     0.8)     1.0]


    X position (horizontal, left-to-right across frame):
    ┌────────┬────────┬────────┬────────┬────────┐
    │ Bin 0  │ Bin 1  │ Bin 2  │ Bin 3  │ Bin 4  │
    │ far    │ left   │ center │ right  │ far    │
    │ left   │        │        │        │ right  │
    └────────┴────────┴────────┴────────┴────────┘
    0.0                  0.5                    1.0


    Y position (vertical, top-to-bottom):
    ┌────────┐  Bin 0: top of frame       (0.0 - 0.2)
    ├────────┤  Bin 1: upper              (0.2 - 0.4)
    ├────────┤  Bin 2: center             (0.4 - 0.6)
    ├────────┤  Bin 3: lower              (0.6 - 0.8)
    └────────┘  Bin 4: bottom of frame    (0.8 - 1.0)


    Size (sqrt-scaled board area as fraction of frame):
    Bin 0 [0.0-0.2): board covers <1.6% of frame    (very far)
    Bin 1 [0.2-0.4): board covers 1.6-6.4% of frame (far)
    Bin 2 [0.4-0.6): board covers 6.4-14.4%         (medium)
    Bin 3 [0.6-0.8): board covers 14.4-25.6%        (close)
    Bin 4 [0.8-1.0]: board covers >25.6%             (very close)

    Note: Size uses sqrt scaling so that typical real-world distances
    spread more evenly across bins. The raw area fractions above are
    the pre-scaling values that map to each bin.


    Skew (perspective distortion from tilting):
    Bin 0 [0.0-0.2): nearly flat / face-on to camera
    Bin 1 [0.2-0.4): slight tilt
    Bin 2 [0.4-0.6): moderate tilt
    Bin 3 [0.6-0.8): strong tilt
    Bin 4 [0.8-1.0]: extreme tilt (>40% aspect ratio deviation)

    Skew is measured as the deviation of the board's observed width/height
    ratio from its known 5:7 aspect ratio. More tilt = more foreshortening
    = higher skew value.
```

### Example: Good vs Bad Coverage

Here is what the histograms might look like after 25 captures:

```
    BAD coverage (all captures from center, same distance, flat):

    X pos:    [  0 |  0 | 25 |  0 |  0 ]    <-- everything in middle bin
    Y pos:    [  0 |  0 | 25 |  0 |  0 ]
    Size:     [  0 |  0 | 25 |  0 |  0 ]
    Skew:     [ 25 |  0 |  0 |  0 |  0 ]    <-- always flat

    Diversity score: ~0.10  (terrible)


    GOOD coverage (varied positions, distances, angles):

    X pos:    [  3 |  5 |  7 |  6 |  4 ]
    Y pos:    [  4 |  6 |  5 |  5 |  5 ]
    Size:     [  4 |  5 |  8 |  5 |  3 ]
    Skew:     [  6 |  5 |  6 |  5 |  3 ]

    Diversity score: ~0.85  (excellent)
```

Visualized as bar charts:

```
    BAD:   X pos                    GOOD:  X pos
    8|                              8|        #
    6|                              6|     #  #  #
    4|                              4|  #  #  #  #  #
    2|                              2|  #  #  #  #  #
    0|  .  . ## .  .               0|  #  #  #  #  #
     +--1--2--3--4--5               +--1--2--3--4--5
```

### Sample Gating

Not every frame is worth capturing. The tracker gates captures to
prevent hoarding samples in already-full bins:

- **Primary gate**: accept if the sample fills a bin that has fewer
  than `min_per_bin` samples (default: 2).
- **Secondary gate**: accept if the bin is below the axis median count
  (improves balance even after minimum is met).
- **Early leniency**: the first 5 captures are always accepted to give
  the user quick positive feedback.

---

## 7. Diversity Score and Completion

### Diversity Score (0.0 to 1.0)

The diversity score combines two measures per axis, then takes a
weighted average across all four axes:

```
    Per-axis score = 0.5 * coverage + 0.5 * normalised_entropy

    Where:
      coverage          = fraction of bins with at least 1 sample
                          (e.g., 4 out of 5 bins touched = 0.80)

      normalised_entropy = Shannon entropy / max possible entropy
                          (measures how evenly spread the samples are)
                          (0 = all in one bin, 1 = perfectly uniform)
```

The per-axis scores are combined with weights:

```
    Axis weights:
      X    = 0.35   (horizontal position matters most)
      Y    = 0.25   (vertical -- harder to reach extremes)
      Size = 0.15   (distance variation)
      Skew = 0.25   (tilt variation)
      ─────────
      Total = 1.00
```

There is also a safety floor: if any axis has less than 25% of its
bins touched, the overall score is capped at 0.85 to prevent
completion with a totally neglected axis.

### is_good_enough(): The Completion Gate

The old approach required every single bin to be filled. That was
frustrating -- some extreme bins (board at the very top-left corner,
heavily tilted) are physically hard to reach. Users would get stuck
chasing the last unfilled bin.

The new `is_good_enough()` uses a softer criterion:

```
    is_good_enough(min_samples=20, score_threshold=0.35):

    1. Have we collected >= min_samples captures?
       NO  --> not done yet
       YES --> continue

    2. Is diversity_score >= score_threshold?
       YES --> DONE (good variety achieved)
       NO  --> continue

    3. Stagnation check:
       Look at diversity scores from last 5 captures.
       Did diversity improve by less than 0.02?
       YES --> DONE (user has tried enough, further captures
                     won't help without a different setup)
       NO  --> keep going
```

The stagnation escape prevents the user from being stuck forever
when the physical setup limits their range of motion.

---

## 8. The Math: How Intrinsics Are Computed

This section explains the mathematical pipeline from detected corners
to the final intrinsic matrix and distortion coefficients.

### 8.1 The Pinhole Camera Model

The foundation is the **pinhole model** -- a simplified camera where
light passes through a single point (the "pinhole") onto an image
plane:

```
    Real world                   Image plane
    3D point P                   2D point p
    (X, Y, Z)                   (u, v)

           P = (X,Y,Z)
            \
             \
              \        focal length f
               \      |<---------->|
                \     |            |
                 *----+------------+---> camera Z axis
              pinhole |            |
              (origin)|      p = (u,v)
                      |            |
                      |  image plane

    The projection:

         u = fx * (X / Z) + cx
         v = fy * (Y / Z) + cy

    In matrix form:

         | u |       | fx   0   cx | | X |
    s *  | v |   =   |  0  fy   cy | | Y |   =  K * [R | t] * P_world
         | 1 |       |  0   0    1 | | Z |

    where:
      s      = scale factor (the depth Z)
      K      = intrinsic matrix (what we want to find)
      [R|t]  = extrinsic matrix (camera position/orientation, per frame)
      P_world = 3D point in world coordinates
```

### 8.2 Lens Distortion Model

Real lenses are not perfect pinholes. They bend light unevenly,
especially at the edges. OpenCV models this with two types of
distortion applied to the *normalised* image coordinates:

```
    Step 1: Normalise
    -----------------
    x' = (X / Z)       y' = (Y / Z)       (ideal pinhole projection)
    r  = sqrt(x'^2 + y'^2)                 (distance from optical axis)


    Step 2: Apply radial distortion
    --------------------------------
    x'' = x' * (1 + k1*r^2 + k2*r^4 + k3*r^6)
    y'' = y' * (1 + k1*r^2 + k2*r^4 + k3*r^6)

    This bends points radially:
      k1 < 0  -->  barrel distortion  (edges pushed inward)
      k1 > 0  -->  pincushion         (edges pushed outward)

    k1 dominates. k2 and k3 add higher-order corrections for
    strong lenses.


    Step 3: Apply tangential distortion
    ------------------------------------
    x''' = x'' + 2*p1*x'*y' + p2*(r^2 + 2*x'^2)
    y''' = y'' + p1*(r^2 + 2*y'^2) + 2*p2*x'*y'

    Caused by the lens not being perfectly parallel to the sensor.
    Usually very small (p1, p2 ~ 0.001).


    Step 4: Convert back to pixel coordinates
    -------------------------------------------
    u = fx * x''' + cx
    v = fy * y''' + cy

    This is the full projection: 3D point --> distorted 2D pixel.
```

### 8.3 Zhang's Method (What cv2.calibrateCamera Does)

The calibration uses **Zhang's method** (Zhang, 2000), the standard
algorithm implemented in OpenCV. Here's how it works step by step:

```
    INPUT:
      - N frames, each with a set of 2D-to-3D point correspondences:
        (u_i, v_i) <--> (X_i, Y_i, 0)
               ^                    ^
          detected corner      known position on the flat board
          in the image         (Z = 0 because the board is planar)
```

**Step A: Homography estimation (per frame)**

Because the calibration board is flat (Z = 0), the 3D-to-2D
projection simplifies to a **homography** -- a 3x3 matrix H that
maps board points directly to image points:

```
    Full projection:

         | u |       | fx  0  cx | | r1  r2  r3  tx | | X |
    s *  | v |   =   | 0  fy  cy | | r4  r5  r6  ty | | Y |
         | 1 |       | 0   0   1 | | r7  r8  r9  tz | | 0 |
                                                       | 1 |
                         K              [R | t]

    Since Z = 0, the third column of R drops out:

         | u |       | fx  0  cx | | r1  r2  tx | | X |
    s *  | v |   =   | 0  fy  cy | | r4  r5  ty | | Y |
         | 1 |       | 0   0   1 | | r7  r8  tz | | 1 |

                   =        H (3x3)             * p_board

    H = K * [r1  r2  t]     (a 3x3 homography)
```

Each frame gives one H. This is computed using the **DLT algorithm**
(Direct Linear Transform) with at least 4 point correspondences,
followed by refinement.

**Step B: Closed-form intrinsics estimation**

Zhang showed that each homography H provides **two constraints** on
the intrinsic matrix K. With N >= 3 frames (6+ constraints), we can
solve for K analytically:

```
    From each homography H = [h1  h2  h3]:

      h1^T * K^-T * K^-1 * h2 = 0           (constraint 1: orthogonality)
      h1^T * K^-T * K^-1 * h1 =              (constraint 2: equal norms)
      h2^T * K^-T * K^-1 * h2

    Let B = K^-T * K^-1  (a symmetric 3x3 matrix with 6 unknowns).
    Each frame gives 2 equations. With N frames:
      2*N equations, 6 unknowns  -->  need N >= 3 frames

    Solve for B using least squares, then extract K from B via
    Cholesky decomposition.
```

This gives an initial estimate of K (fx, fy, cx, cy).

**Step C: Extrinsics estimation (per frame)**

With K known, compute R and t for each frame using **PnP**
(Perspective-n-Point):

```
    Given: K, known 3D points, observed 2D points
    Find:  R (rotation), t (translation) of the board relative to camera

    PnP finds the pose that minimises:

      sum_i || (u_i, v_i) - project(K, R, t, P_i) ||^2

    OpenCV uses solvePnP() which implements:
      - P3P (minimal solver, 3 points) for initialisation
      - Iterative refinement (Levenberg-Marquardt) for precision

    This is the same solvePnP we use during capture for live pose
    estimation (see detector.py).
```

**Step D: Non-linear refinement (bundle adjustment)**

The closed-form solution from Steps B-C is a good starting point
but not optimal. The final step refines ALL parameters simultaneously
using **Levenberg-Marquardt** optimisation:

```
    Minimise over (K, k1..k3, p1..p2, R_1..R_N, t_1..t_N):

                    N     M_j
      total_err =  SUM   SUM  || p_ij - project(K, dist, R_j, t_j, P_i) ||^2
                   j=1   i=1

    Where:
      N     = number of frames
      M_j   = number of detected corners in frame j
      p_ij  = detected 2D position of corner i in frame j
      P_i   = known 3D position of corner i on the board

      project() = the full pinhole + distortion model from Section 8.2

    Levenberg-Marquardt:
      - A blend of gradient descent and Gauss-Newton
      - Iteratively adjusts all parameters to reduce total_err
      - Converges to a local minimum (which is usually global for
        well-conditioned calibration data)

    Typically converges in 10-30 iterations.
```

### 8.4 What Happens in Our Code

Here is how the math maps to the code:

```
    detector.py: estimate_pose()
    ============================
    board.matchImagePoints(corners, ids)
      --> matches detected 2D corners to known 3D board positions

    cv2.solvePnP(obj_pts, img_pts, K, dist)
      --> PnP solve (Step C above)
      --> returns rvec (Rodrigues rotation), tvec (translation)
      --> used during capture for live pose display + orientation detection

    Note: rvec is a compact 3x1 rotation vector. Convert to a 3x3
    rotation matrix R with:  R = cv2.Rodrigues(rvec)


    calibrator.py: calibrate_charuco()
    ===================================
    board.matchImagePoints(corners, ids)    [for each frame]
      --> builds paired lists of (obj_points, img_points)

    cv2.calibrateCamera(obj_points, img_points, frame_size, None, None)
      --> runs the full Zhang's method (Steps A through D)
      --> returns: rms, K, dist, rvecs, tvecs

    The "None, None" arguments mean: no initial guess for K or dist,
    let OpenCV compute everything from scratch.


    calibrator.py: compute_per_frame_errors_charuco()
    ==================================================
    For each frame:
      cv2.projectPoints(obj_pts, rvec, tvec, K, dist)
        --> forward-projects 3D board corners through the solved model
        --> compares to the actually detected 2D corners
        --> per-frame RMS = sqrt(mean(squared_distances))

    This is the "reprojection error" -- the gold-standard quality metric.
```

### 8.5 The Reprojection Error

The RMS reprojection error measures how well the solved model
explains the observed data:

```
                   +-----------+                  +-----------+
                   | 3D board  |                  | 3D board  |
                   | corner    |                  | corner    |
                   | (known)   |                  | (known)   |
                   +-----+-----+                  +-----+-----+
                         |                              |
                         | actual light path             | model's prediction
                         | through real lens             | using solved K, dist
                         v                              v
                   +-----------+                  +-----------+
                   | detected  |                  | projected |
                   | corner    |                  | corner    |
                   | (u_d,v_d) |                  | (u_p,v_p) |
                   +-----------+                  +-----------+
                         |                              |
                         +---------> error <------------+
                              || (u_d,v_d) - (u_p,v_p) ||

    RMS = sqrt( mean of all squared errors across all corners, all frames )

    Interpretation:
      < 0.5 px  :  Excellent -- sub-pixel accuracy
      < 1.0 px  :  Good -- sufficient for most applications
      > 1.0 px  :  Poor -- consider recalibrating with better coverage
```

### 8.6 Why Diverse Poses Matter

The math reveals why the quality tracker insists on diversity:

```
    If all frames are from the same position / distance / angle:

      - Homographies H_1 ... H_N are nearly identical
      - The 2*N constraints in Step B are nearly redundant
      - K is under-constrained --> fx/fy and cx/cy become ambiguous
      - Distortion coefficients are poorly estimated at frame edges
        (because no data exists there)

    With diverse poses:

      - Each H provides genuinely independent constraints
      - K is well-determined (over-constrained)
      - Distortion is sampled across the full lens
      - The Levenberg-Marquardt refinement converges faster and to
        a better minimum
```

This is why the earlier "stable-mode" calibration (mostly
bottom-center, same distance) gave fx=1014 while the diverse
auto-capture run gave fx=979 -- the first was under-constrained
and the optimizer couldn't properly separate focal length from
distortion.

---

## 9. Two-Pass Calibration with Outlier Rejection

Once enough samples are collected, the system runs a two-pass
calibration to get the best possible result:

```
    Pass 1: Calibrate with ALL captured frames
    ==========================================

    all_corners + all_ids
           |
           v
    cv2.calibrateCamera()
           |
           v
    K, dist, rvecs, tvecs, RMS_1
           |
           v
    Compute per-frame reprojection error
    (project 3D points back to 2D, measure distance from detected corners)


    Outlier Detection
    =================

    For each frame, compute its individual RMS error.
    If frame_error > outlier_threshold (default 0.5 px):
       --> mark as outlier

    Example:
      Frame  1:  0.12 px   OK
      Frame  2:  0.08 px   OK
      Frame  3:  0.95 px   OUTLIER  <-- maybe the board moved during capture
      Frame  4:  0.15 px   OK
      ...


    Pass 2: Recalibrate WITHOUT outliers
    ====================================

    clean_corners + clean_ids   (outliers removed)
           |
           v
    cv2.calibrateCamera()       (second run)
           |
           v
    K, dist, rvecs, tvecs, RMS_2   (usually lower than RMS_1)
```

### Why two passes?

Even with stability checking, some captured frames may have subtle
issues -- slight motion blur, partially occluded corners, or
interpolation errors. A single bad frame can pull the calibration
solution away from the optimum.

By running once, identifying statistical outliers, removing them,
and running again, the final result is more robust. The system
requires at least 10 clean frames after outlier removal; if too
many are rejected, it falls back to keeping all frames.

---

## 10. Output

The system produces three output files:

### calibration.json

Human-readable JSON with all calibration parameters:

```json
{
  "camera_intrinsics": {
    "focal_length": { "fx": 912.34, "fy": 911.78 },
    "principal_point": { "cx": 641.22, "cy": 358.91 },
    "intrinsic_matrix": [[912.34, 0, 641.22], ...]
  },
  "distortion_coefficients": {
    "k1": -0.04231, "k2": 0.08912,
    "p1": 0.00012, "p2": -0.00034,
    "k3": -0.03218
  },
  "calibration_quality": {
    "rms_reprojection_error_px": 0.312,
    "quality": "excellent"
  },
  "frame_size": { "width": 1280, "height": 720 }
}
```

### calibration.npz

NumPy binary archive for programmatic use:

```python
import numpy as np

data = np.load("calibration.npz")
K    = data["camera_matrix"]     # (3, 3) intrinsic matrix
dist = data["dist_coeffs"]       # (1, 5) distortion coefficients
rms  = float(data["rms_error"])  # scalar
size = data["frame_size"]        # [width, height]
```

### calibration_verification.jpg

A three-panel image showing the calibration effect:

```
    +--------------------+--------------------+--------------------+
    |                    |                    |                    |
    |   ORIGINAL         |  UNDISTORTED       |  UNDISTORTED       |
    |   (distorted)      |  alpha=0           |  alpha=1           |
    |                    |  (cropped, no      |  (full frame,      |
    |                    |   black borders)   |   green = valid    |
    |                    |                    |   ROI)             |
    +--------------------+--------------------+--------------------+
    | fx=912.34  fy=911.78  cx=641.22  cy=358.91                  |
    | k1=-0.04231  k2=0.08912  p1=0.00012  p2=-0.00034           |
    +-------------------------------------------------------------+
```

### Understanding the intrinsic matrix

```
            | fx   0   cx |
    K   =   |  0  fy   cy |
            |  0   0    1 |

    fx, fy:  Focal length in pixels.
             A pixel at the image edge is fx (or fy) pixels away
             from a ray that passes through the lens centre and
             hits the sensor one unit away. Larger values = more zoom.

    cx, cy:  Where the lens optical axis pierces the sensor plane.
             For a 1280x720 image, expect roughly cx~640, cy~360.
```

### Understanding distortion coefficients

```
    k1, k2, k3:  Radial distortion.
                  Controls barrel (k1 < 0) or pincushion (k1 > 0).
                  k1 dominates; k2 and k3 refine the model for
                  stronger lenses.

    p1, p2:       Tangential distortion.
                  Caused by slight misalignment between the lens
                  and the sensor. Usually very small.
```

### Quality benchmarks

```
    RMS < 0.5 px   -->  Excellent  (suitable for 3D reconstruction)
    RMS < 1.0 px   -->  Good       (suitable for most applications)
    RMS > 1.0 px   -->  Poor       (consider recalibrating)
```

### How to use the calibration

```python
import cv2
import numpy as np

# Load
data = np.load("calibration.npz")
K    = data["camera_matrix"]
dist = data["dist_coeffs"]

# Undistort a frame
frame = cv2.imread("photo.jpg")
corrected = cv2.undistort(frame, K, dist)

# Or build undistort maps (faster for video -- compute once, apply many):
h, w = frame.shape[:2]
new_K, roi = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), alpha=0)
mapx, mapy = cv2.initUndistortRectifyMap(K, dist, None, new_K, (w, h), cv2.CV_32FC1)

for frame in video_frames:
    corrected = cv2.remap(frame, mapx, mapy, cv2.INTER_LINEAR)
```

---

## 11. Cross-Platform Support

### Text-to-Speech Backends

Voice guidance uses the first available engine on the platform:

```
    Platform    Engine              Notes
    --------    ------              -----
    macOS       say                 Built-in, best quality
    Linux       espeak              Widely available
                spd-say             Speech Dispatcher
                festival            Fallback
    Windows     SAPI (PowerShell)   System.Speech.Synthesis
```

If no TTS engine is found, the system continues without voice.
Visual feedback (HUD overlay + iPad border) still works.

All TTS calls are non-blocking (run in background threads) so they
never stall the capture loop.

### macOS Screen Auto-Detection

On macOS, the system can query Quartz/CoreGraphics to determine the
physical dimensions of the attached display. This auto-computes the
board width when the pattern is displayed on the local screen
(not needed in iPad mode).

### iPad Mode (Flask Web Server)

The web server provides:

```
    Endpoint        Purpose
    --------        -------
    /               iPad pattern page (ChArUco + border feedback)
    /pattern.png    Raw board image
    /events         SSE stream (detection state, instructions, count)
    /feed           MJPEG stream (annotated camera feed)
    /monitor        HTML page to view the MJPEG feed
```

The server runs on a background daemon thread. SSE uses a
per-client queue with a maxsize of 60 messages, with automatic
cleanup of dead clients. The MJPEG feed is capped at ~20 fps.

### Board Width in iPad Mode

In iPad mode the user supplies `--board_width_mm` directly, measured
with a ruler from the corner of the outermost square to the corner of
the opposite outermost square along the wider edge of the rendered
pattern. An earlier version of this tool maintained a database of
known iPad models and computed the displayed pattern width from each
device's nominal CSS viewport, but this was removed: the actual
viewport varies with fullscreen state, orientation, Display Zoom,
Split-View, and Safari URL-bar visibility, none of which are
reliably detectable from server-side code, and a ruler is more
accurate than any database.

This value is also less critical than it sounds. Camera calibration
via Zhang's method is **scale-invariant in the object points** (see
§8.3): if every 3D board coordinate is scaled by α, the homography
absorbs that factor entirely into the per-view translation vector.
The intrinsic matrix `K` and distortion coefficients are unchanged.
So an inaccurate `--board_width_mm` only rescales the returned
`tvecs` — fx, fy, cx, cy, k1, k2, k3, p1, p2 are unaffected.

---

## Quick Reference

```
    Typical usage (auto-capture mode, default):

    python main.py --source rtsp://192.168.1.50:554/stream \
                   --ipad --board_width_mm 133

    1. Open http://<laptop-ip>:8080/ on the iPad
    2. Tap to enter fullscreen
    3. Slowly wave the iPad in front of the camera
    4. Watch the border color:
         RED    = not visible, keep moving
         AMBER  = detected, keep moving slowly
         GREEN flash = captured! keep moving to a new position
    5. Vary position (left/right/up/down), distance (near/far),
       and angle (tilt in different directions)
    6. Captures happen automatically as you cover new regions
       (no need to hold still!)
    7. System stops automatically when diversity is sufficient
    8. Results saved to calibration.json, .npz, and _verification.jpg
```

---

*Generated from source analysis of MDCT-Proto calibration system.*
