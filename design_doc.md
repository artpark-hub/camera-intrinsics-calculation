

# Design Spec: MDCT-Proto (Mac-based Dynamic Calibration Target)

## 1. Project Goal
Create a Python-based prototype that acts as a **Mobile Dynamic Calibration Target**. The Mac displays a calibration pattern, ingests a remote RTSP security camera feed, detects its own display within that feed, and guides the user through 3D pose captures via voice to calculate camera intrinsics.

## 2. Technical Stack
* **Language:** Python 3.10+
* **Core Library:** `opencv-contrib-python` (Required for ArUco/ChArUco modules).
* **Math:** `NumPy`.
* **Audio/Voice:** `os.system('say ...')` (macOS native TTS).
* **Streaming:** FFmpeg-backed OpenCV `VideoCapture`.

---

## 3. Functional Modules

### A. Pattern Display Engine
* **Target:** Create a high-contrast **ChArUco Board**.
* **Parameters:** * Squares: 5x7
    * Marker Size: 0.02m (20mm)
    * Square Size: 0.04m (40mm)
    * Dictionary: `DICT_4X4_50`
* **UI:** A borderless CV2 window that stays "Always on Top."

### B. Remote Feed Ingestion
* **Input:** RTSP URL (e.g., `rtsp://admin:password@192.168.1.100:554/stream1`).
* **Processing:** Resize the incoming 4K/1080p stream to a workable resolution (e.g., 720p) for lower latency during detection.

### C. Vision & Pose Estimation Logic
* **Detector:** `cv2.aruco.CharucoBoard_create` and `cv2.aruco.interpolateCornersCharuco`.
* **State Tracking:** * Maintain a list of `all_corners` and `all_ids`.
    * Calculate **Reprojection Error** in real-time once $>10$ frames are captured.
* **Stability Logic:** Only capture a "Pose" if corners are detected and the variance in corner positions over 30 frames is $< 2.0$ pixels.

### D. Voice Guidance System (HMI)
* **Spatial Logic:** Divide the camera FOV into 9 sectors (Top-Left, Center, etc.).
* **Rotation Logic:** Use the estimated Rotation Vector ($rvec$) to determine if the board is flat or tilted.
* **Instruction Set:**
    * "Move the iPad to the top-right of the camera view."
    * "Tilt the screen toward the ground."
    * "Great, hold steady... Captured."

---

## 4. Execution Workflow
1.  **Init:** User inputs the RTSP URL.
2.  **Calibration Loop:**
    * Display ChArUco on Mac screen.
    * Read RTSP frame -> Detect Corners.
    * If Corners $> 6$: Show 3D axis overlay on the remote feed window.
    * Check if the current "Pose" (Position + Angle) is unique compared to previous captures.
    * If unique and stable: **Voice Trigger** -> "Capturing Pose X of 20."
3.  **Completion:**
    * Run `cv2.aruco.calibrateCameraCharuco`.
    * Export `intrinsics.json` (K-matrix, Distortion).

---

## 5. Antigravity Implementation Tasks

### Task 1: The Core Detector
> "Write a Python script using OpenCV to display a 5x7 ChArUco board. Simultaneously open an RTSP stream. Detect the board in the stream and draw the detected corners and a 3D coordinate axis on the board's center in the remote feed window."

### Task 2: Guidance State Machine
> "Implement a logic controller that tracks 'captured zones.' Define 5 required orientations: Flat-Center, Tilt-Up, Tilt-Down, Tilt-Left, Tilt-Right. Use macOS 'say' command to instruct the user to move the screen until all 5 orientations are captured."

### Task 3: Data Export
> "Add a calibration routine that executes once 20 samples are collected. Output the Focal Length (fx, fy), Principal Point (cx, cy), and 5 distortion coefficients to a JSON file."

---

## 6. Testing Constraints
* **Physical Measurement:** The user MUST provide the physical width of the displayed board on the Mac monitor in millimeters for the math to be valid.
* **Latency:** Include a `cv2.waitKey(1)` buffer to ensure the display window doesn't freeze while the RTSP stream is being processed.

---

