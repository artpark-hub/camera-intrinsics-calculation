#!/usr/bin/env python3
"""
MDCT-Proto — Dynamic Calibration Target
========================================
Displays a ChArUco board (on a local screen **or** an iPad via web),
ingests a remote RTSP camera feed, detects the board, guides the user
through 3D pose captures via voice + visual feedback, and computes
camera intrinsics.

Two-device mode  (recommended)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The ChArUco pattern is served to an iPad (or any tablet/phone) over
the local network.  The iPad shows real-time coloured-border feedback
so the user knows whether the camera can see the board — without
having to look at the laptop.

    python main.py --source rtsp://… --ipad --board_width_mm 133

Single-screen mode  (original)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    python main.py --source rtsp://…
    python main.py --source http://mediamtx-host:8888/ART-65/index.m3u8
    python main.py --video test_video.mp4
    python main.py --camera 0
"""

import argparse
import cv2
import numpy as np
import signal
import sys
import time
from collections import deque

from pattern_display import (
    create_board,
    PatternWindow,
    render_board_image,
    render_board_png_bytes,
)
from feed_ingestion import open_source, resize_frame, read_frame, ThreadedCapture
from detector import CharucoDetector
from quality_tracker import QualityTracker
from guidance import VoiceGuidance
from calibrator import calibrate_charuco, compute_per_frame_errors_charuco
from overlay import draw_hud
from screen_measure import (
    auto_detect_board_width,
    print_screen_info,
)
from exporter import (
    export_intrinsics_json,
    export_calibration_npz,
    save_verification_image,
    print_results,
)
from confidence import compute_confidence, print_confidence_summary


# ── Live (in-loop) calibration confidence check ─────────────────────────────

def run_trial_confidence(all_corners, all_ids, board, frame_size, tracker,
                         outlier_rms):
    """
    Run a silent trial calibration on the captures collected so far and
    return the computed confidence dict, plus the live fx for HUD display.

    Returns (confidence_dict, rms, fx) on success, or (None, None, None)
    if the solver failed (typically: too few valid obj/img point pairs).
    """
    try:
        rms, K, dist, _rvecs, _tvecs, _uc, _uid, health = calibrate_charuco(
            all_corners, all_ids, board, frame_size,
            outlier_rms=outlier_rms,
            quiet=True,
        )
    except Exception as e:
        return (None, None, None, str(e))

    conf = compute_confidence(
        rms=rms, K=K, dist=dist,
        health=health,
        tracker_dict=tracker.as_dict(),
        per_frame_errors=None,
    )
    return (conf, float(rms), float(K[0, 0]), None)


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="MDCT-Proto: Dynamic Calibration Target",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  # iPad auto-capture (recommended — just wave the iPad slowly)\n"
            "  python main.py --source rtsp://… --ipad --board_width_mm 133\n\n"
            "  # Via a MediaMTX republish (when the camera is on a different subnet)\n"
            "  python main.py --source rtsp://mediamtx-host:8554/ART-65 "
            "--ipad --board_width_mm 133\n\n"
            "  # Via HLS playlist (works through firewalls; adds 1-3 s latency)\n"
            "  python main.py --source http://mediamtx-host:8888/ART-65/index.m3u8 "
            "--ipad --board_width_mm 133\n\n"
            "  # Timed capture — one capture every 3 seconds\n"
            "  python main.py --source rtsp://… --ipad --board_width_mm 133 "
            "--capture_mode timed --capture_interval 3\n\n"
            "  # Legacy stable mode (requires holding iPad still)\n"
            "  python main.py --source rtsp://… --ipad --board_width_mm 133 "
            "--capture_mode stable\n\n"
            "  # Local-screen mode (pattern on Mac, board width auto-detected)\n"
            "  python main.py --source rtsp://… --board_width_mm 280\n"
        ),
    )

    # Input source (mutually exclusive)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--source", "--rtsp", dest="source", type=str,
        metavar="URL",
        help="Stream URL — anything FFmpeg can open: "
             "rtsp://…, http(s)://…/index.m3u8 (HLS), http://…/stream (MJPEG), "
             "or a MediaMTX republish like rtsp://mediamtx-host:8554/<name>. "
             "(`--rtsp` is kept as a backwards-compatible alias.)",
    )
    src.add_argument("--video",  type=str, help="Path to a local video file")
    src.add_argument("--camera", type=int, help="Webcam device index")

    # ── iPad / two-device mode ───────────────────────────────────────────────
    parser.add_argument(
        "--ipad", action="store_true",
        help="Serve the ChArUco pattern to an iPad/tablet via web "
             "(enables two-device mode with visual feedback). Open "
             "http://<your-laptop-ip>:8080/ on the iPad in Safari.",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Web-server port for iPad mode (default: 8080)",
    )

    # Board display
    parser.add_argument(
        "--board_width_mm", type=float, default=0,
        help="Physical width (mm) of the displayed ChArUco pattern, measured "
             "with a ruler from the corner of the outermost square to the "
             "corner of the opposite outermost square. Required for iPad "
             "mode. In local-screen mode on macOS it will be auto-detected "
             "from Quartz if not specified. NOTE: Zhang's method is "
             "scale-invariant in object points, so this value affects only "
             "the absolute scale of returned tvecs — never fx, fy, cx, cy "
             "or the distortion coefficients.",
    )
    parser.add_argument(
        "--display_width", type=int, default=0,
        help="Pixel width of the rendered ChArUco image "
             "(default: 1200 for iPad, 900 for local)",
    )

    # Calibration parameters
    parser.add_argument("--min_captures", type=int, default=20,
                        help="Minimum number of pose captures (default: 25)")
    parser.add_argument("--bins", type=int, default=5,
                        help="Number of bins per quality axis (default: 5)")
    parser.add_argument("--min_per_bin", type=int, default=2,
                        help="Samples required per bin (default: 2)")
    parser.add_argument("--outlier_rms", type=float, default=0.5,
                        help="Per-frame RMS threshold for outlier rejection (default: 0.5)")
    parser.add_argument("--diversity_threshold", type=float, default=0.75,
                        help="Diversity score (0-1) needed for completion (default: 0.75). "
                             "Lower = easier to finish, higher = more coverage required.")
    parser.add_argument("--confidence_threshold", type=float, default=85.0,
                        help="Minimum confidence score (0-100) required to finish "
                             "(default: 85 = 'Good' / B grade). The capture loop runs "
                             "trial calibrations past --min_captures and only exits once "
                             "confidence clears this threshold (or --max_captures is hit). "
                             "Handheld iPad calibration is structurally capped around 91 "
                             "because the edge_gap floor is physical (bezel + marker quiet "
                             "zone), so 95+ is essentially unreachable without a printed "
                             "board on a flat desk.")
    parser.add_argument("--min_axis_coverage", type=float, default=0.80,
                        help="Minimum fraction of bins that must be touched on EVERY axis "
                             "before completion is allowed (default: 0.80).")
    parser.add_argument("--max_captures", type=int, default=80,
                        help="Hard cap on number of captures (default: 80). Safety net so "
                             "an aggressive --confidence_threshold can't trap the user in "
                             "an infinite capture loop.")
    parser.add_argument("--trial_calibration_every", type=int, default=3,
                        help="Run a trial calibration every N captures past --min_captures "
                             "to check confidence (default: 3). Smaller = more responsive "
                             "but more CPU.")
    parser.add_argument("--user_facing_camera",
                        type=lambda v: v.lower() not in ("false", "0", "no", "off"),
                        default=True,
                        help="Whether the user is standing FACING the camera (default: true). "
                             "When true, the camera image is mirrored relative to the user's "
                             "body, so voice prompts flip 'left' and 'right' to match what "
                             "the user's body should actually do. Pass --user_facing_camera "
                             "false for selfie-style or moving-with-camera setups.")

    # Capture mode
    parser.add_argument(
        "--capture_mode", type=str, default="auto",
        choices=["auto", "timed", "stable"],
        help="Capture strategy: "
             "'auto' = captures sharpest frame when board enters a new quality "
             "region (just wave the iPad slowly); "
             "'timed' = captures every --capture_interval seconds; "
             "'stable' = original mode requiring board to be held still. "
             "(default: auto)",
    )
    parser.add_argument(
        "--capture_interval", type=float, default=2.0,
        help="Seconds between captures in 'timed' mode (default: 2.0)",
    )

    # Detection (used by 'stable' capture mode)
    parser.add_argument("--stability_frames", type=int, default=8,
                        help="Frames to track for stability check (default: 8)")
    parser.add_argument("--stability_threshold", type=float, default=5.0,
                        help="Max corner variance (px) for stability (default: 5.0)")

    # Output
    parser.add_argument("--output", type=str, default="calibration",
                        help="Output file base name (default: calibration)")

    # Misc
    parser.add_argument("--no_voice", action="store_true",
                        help="Disable voice guidance")
    parser.add_argument("--no_board", action="store_true",
                        help="Don't display ChArUco board (use external pattern)")

    # Performance / processing
    parser.add_argument(
        "--proc_height", type=int, default=0,
        help="Optional: downscale frames to this height before detection. "
             "0 (default) = preserve native source resolution. "
             "WARNING: any value > 0 means the resulting intrinsics describe "
             "the *resized* frame, not the camera, and sub-pixel corner "
             "localization is degraded. Only use this if you are CPU-bound "
             "and accept reduced accuracy.",
    )

    return parser.parse_args()


# ── Main calibration loop ───────────────────────────────────────────────────

def main():
    args = parse_args()

    # ── Defaults that depend on mode ─────────────────────────────────────────
    ipad_mode = args.ipad
    if args.display_width == 0:
        args.display_width = 1200 if ipad_mode else 900

    # ── 1. Create ChArUco board ──────────────────────────────────────────────
    board, aruco_dict = create_board()
    print(f"[INFO] ChArUco board: 5x7 squares, DICT_4X4_50")

    # ── 2a. iPad mode — start web server & serve pattern ─────────────────────
    web_push = None  # Will be set to a callable if iPad mode is active

    if ipad_mode:
        from web_server import (
            start_server,
            set_pattern_png,
            update_detection_state,
            update_feed_frame,
        )

        # Render high-res pattern and hand it to the web server
        png_bytes = render_board_png_bytes(board, args.display_width)
        set_pattern_png(png_bytes)

        # Start the web server (background thread)
        local_ip, port = start_server(port=args.port)

        print(f"[INFO] Open  http://{local_ip}:{port}/  on your iPad")
        print(f"       then tap the screen to enter fullscreen.\n")

        # Convenience wrapper for pushing state to iPad
        def web_push_state(state, instruction="", samples=0):
            update_detection_state(state, instruction, samples)

        web_push = web_push_state
        web_push_frame = update_feed_frame

    # ── 2b. Local-screen mode — show OpenCV pattern window ───────────────────
    pattern_window = None
    if not ipad_mode and not args.no_board:
        pattern_window = PatternWindow(board, args.display_width)
        pattern_window.show()
        cv2.waitKey(500)
        print(f"[INFO] Board displayed ({args.display_width}px wide)")

    # ── 3. Determine physical board width ────────────────────────────────────
    if args.board_width_mm > 0:
        print(f"[INFO] Physical board width: {args.board_width_mm:.1f} mm "
              f"(user-specified)")
    elif not ipad_mode:
        # Single-screen mode on macOS — try Quartz auto-detection
        print_screen_info()
        board_img = render_board_image(board, args.display_width)
        detected_mm = auto_detect_board_width(args.display_width, board_img)
        if detected_mm:
            args.board_width_mm = detected_mm
            print(f"[INFO] Physical board width: {args.board_width_mm:.1f} mm "
                  f"(auto-detected from Mac screen)")
        else:
            print("[WARN] Could not auto-detect screen dimensions.")
            print("       Pass --board_width_mm <measured> for accurate "
                  "real-world distances.")
    else:
        # iPad mode without --board_width_mm
        print("[WARN] No --board_width_mm specified for iPad mode.")
        print("       Measure the displayed ChArUco pattern with a ruler "
              "(corner of outermost square to corner of opposite outermost "
              "square) and pass via --board_width_mm.")
        print("       NOTE: this value only affects the absolute scale of "
              "the returned tvecs — Zhang's method is scale-invariant in")
        print("       object points, so fx, fy, cx, cy and the distortion "
              "coefficients are determined regardless. Calibration will")
        print("       still proceed with default object-point units.")

    # ── 4. Open feed ─────────────────────────────────────────────────────────
    source_type, source, frame_size = open_source(
        rtsp=args.source, video=args.video, camera=args.camera
    )
    frame_w, frame_h = frame_size
    print(f"[INFO] Source frame size : {frame_w}x{frame_h}")
    if args.proc_height > 0 and args.proc_height < frame_h:
        proc_w = int(frame_w * args.proc_height / frame_h)
        print(f"[WARN] --proc_height={args.proc_height} → frames will be DOWNSCALED to "
              f"{proc_w}x{args.proc_height} before detection.")
        print(f"[WARN] The resulting intrinsics will describe the {proc_w}x{args.proc_height} "
              f"frame, NOT the native {frame_w}x{frame_h} camera. Scale fx, fy, cx, cy by "
              f"{frame_h/args.proc_height:.3f}× before applying to full-resolution frames, "
              f"or (better) re-run without --proc_height.")
        proc_h_for_calib = args.proc_height
    else:
        print(f"[INFO] Processing at native resolution (no downscale).")
        proc_h_for_calib = frame_h

    # ── 5. Initialise modules ────────────────────────────────────────────────
    det = CharucoDetector(
        board, aruco_dict,
        stability_frames=args.stability_frames,
        stability_threshold=args.stability_threshold,
    )
    tracker = QualityTracker(n_bins=args.bins, min_per_bin=args.min_per_bin)
    guide = VoiceGuidance(
        min_captures=args.min_captures,
        user_facing_camera=args.user_facing_camera,
    )

    if args.no_voice:
        guide._tts = None  # disable TTS backend

    # Rough camera matrix for pose estimation during capture
    est_focal = max(frame_w, frame_h)
    camera_matrix = np.array([
        [est_focal, 0, frame_w / 2],
        [0, est_focal, frame_h / 2],
        [0, 0, 1],
    ], dtype=np.float64)
    dist_coeffs = np.zeros((1, 5), dtype=np.float64)

    # Storage
    all_corners = []
    all_ids = []
    last_capture_time = 0
    capture_cooldown = 1.5 if args.capture_mode == "stable" else 0.8
    last_suggestion_time = 0
    suggestion_interval = 8.0
    sample_frame = None

    # Rolling buffer for auto-capture mode: keeps recent detections so we
    # can pick the sharpest frame when the board enters a new quality region.
    frame_buffer = deque(maxlen=15)  # ~0.5s at 30fps

    # Live-confidence gate state: every N captures past min_captures we run
    # a silent trial calibration and check the confidence score. The loop
    # exits only once the score clears args.confidence_threshold AND the
    # per-axis coverage clears args.min_axis_coverage, or we hit
    # args.max_captures (the safety cap).
    last_trial_n = 0
    live_confidence: float | None = None
    live_fx: float | None = None
    live_rms: float | None = None

    # Auto-mode "lock" state. When a useful pose first appears, we speak
    # "Hold still" and then wait LOCK_HOLD seconds before actually grabbing
    # the sharpest frame — giving the user time to freeze. This is the
    # only feedback the user gets since they can see neither the HUD nor
    # the iPad border feedback during a calibration run.
    LOCK_HOLD = 0.3   # seconds between "Hold still" and the actual capture
    # Fallback: if `is_useful` has been continuously true for this long
    # WITHOUT ever satisfying momentarily_stable, capture anyway. This
    # prevents the lock from starving when the user is sweeping smoothly
    # enough to never trip the variance gate. The "pick sharpest of last
    # N frames" stage already protects against motion blur, so we don't
    # actually need a hard stillness precondition here.
    USEFUL_HOLD_FALLBACK = 1.0   # seconds
    lock_start_time = 0.0
    lock_announced = False
    useful_since = 0.0

    # Fires once, after we've given up on enough bins that the user almost
    # certainly needs to physically reposition (e.g. get much closer to the
    # camera) rather than keep sweeping from the same spot.
    struggle_announced = False

    print(f"\n[INFO] Calibration loop started. "
          f"Target: confidence ≥ {args.confidence_threshold:.0f}/100  "
          f"(min {args.min_captures}, max {args.max_captures} captures)")
    print(f"[INFO] Capture mode: {args.capture_mode}")
    print(f"[INFO] Quality bins: {args.bins} bins/axis × min {args.min_per_bin} "
          f"samples/bin  |  min-axis-coverage gate: "
          f"{args.min_axis_coverage:.0%}")
    print(f"[INFO] Trial calibrations every {args.trial_calibration_every} "
          f"captures past the minimum.")
    print("[INFO] Press 'q' to quit (keeps current data), "
          "'c' to force-capture, 'r' to reset\n")

    # Orientation primer — must be heard before any directional prompt so
    # the user has the right mental model of "your left" / "your right".
    if args.user_facing_camera:
        orientation_msg = (
            "Important. Stand facing the camera. "
            "When I say your left or your right, I mean your physical body, "
            "not the camera image. The camera will see you mirrored, "
            "but you should always move toward your own left or right hand. "
            "Start by standing about 50 to 80 centimetres from the camera. "
            "At that distance you can easily sweep the iPad to every edge of "
            "the camera view without walking around the room."
        )
    else:
        orientation_msg = (
            "Important. You are moving with the camera, not facing it. "
            "Your left and right match the camera image directly. "
            "Begin about 50 to 80 centimetres from the scene so the board "
            "can reach every corner of the camera view."
        )
    print(f"\n[INFO] {orientation_msg}\n")
    guide._say(orientation_msg, rate=165)
    guide.wait_for_speech(timeout=12.0)
    guide._last_voice_time = time.time()

    if args.capture_mode == "auto":
        guide._say("Calibration started. Slowly wave the screen around "
                   "the camera view. Captures happen automatically.",
                   rate=170)
    elif args.capture_mode == "timed":
        guide._say("Calibration started. Move the screen into the camera view. "
                   f"Capturing every {args.capture_interval:.0f} seconds.",
                   rate=170)
    else:
        guide._say("Calibration started. Move the screen into the camera view.",
                   rate=170)
    if web_push:
        web_push("no_detection",
                 "Calibration started — move the iPad into the camera view",
                 0)

    # ── 6. Main loop ─────────────────────────────────────────────────────────
    FEED_WINDOW = "MDCT Remote Feed"
    cv2.namedWindow(FEED_WINDOW, cv2.WINDOW_NORMAL)

    running = True
    frame_count = 0

    # SIGINT / Ctrl-C: gracefully request loop exit so the collected
    # captures still proceed into the final calibration. cv2.waitKey('q')
    # only works when the OpenCV window has focus — which is useless when
    # the user is standing in front of the camera (facing away from the
    # laptop) and operating entirely by voice. Ctrl-C in the terminal is
    # their only reliable off-switch.
    def _handle_sigint(signum, frame_arg):
        nonlocal running
        if running:
            print("\n[INFO] Ctrl-C received — finishing up and running "
                  "calibration on collected data...")
            running = False
        else:
            # Second Ctrl-C → hard abort
            print("\n[INFO] Second Ctrl-C — aborting without calibration.")
            sys.exit(130)
    signal.signal(signal.SIGINT, _handle_sigint)
    print("[INFO] Press Ctrl-C in THIS terminal to finish the run at "
          "any time (the 'q' key only works if the preview window has "
          "focus, which it won't if you're standing in front of the "
          "camera). Collected data is always used.")

    while running:
        ret, frame = read_frame(source_type, source)
        if not ret or frame is None:
            if source_type == "video":
                print("\n[INFO] End of video.")
                break
            time.sleep(0.01)
            continue

        frame_count += 1
        proc_frame = resize_frame(frame, target_h=args.proc_height)
        ph, pw = proc_frame.shape[:2]

        # Detect ChArUco
        detection = det.detect(proc_frame)
        rvec, tvec = None, None

        if detection.found and detection.num_corners >= 6:
            # ── Board detected ───────────────────────────────────────────
            pose_ok, rvec, tvec = det.estimate_pose(
                proc_frame, detection, camera_matrix, dist_coeffs
            )
            metrics = QualityTracker.compute_metrics(
                detection.charuco_corners, pw, ph
            )

            cx = detection.charuco_corners.reshape(-1, 2)[:, 0].mean()
            cy = detection.charuco_corners.reshape(-1, 2)[:, 1].mean()
            guide.on_detection(cx, cy, pw, ph, rvec)

            now = time.time()
            can_capture = (now - last_capture_time) > capture_cooldown

            is_useful = (tracker.is_useful(metrics)
                         or tracker.total_samples() < 5)

            # ── Capture decision depends on mode ─────────────────────────
            do_capture = False
            cap_corners = detection.charuco_corners
            cap_ids = detection.charuco_ids
            cap_frame = proc_frame
            cap_ncorners = detection.num_corners

            if args.capture_mode == "auto":
                # Auto mode: buffer recent frames; when a useful pose
                # appears, announce "Hold still", wait LOCK_HOLD seconds
                # so the user has time to freeze, and THEN pick the
                # sharpest frame from the now-stabilised buffer.
                sharpness = det.board_sharpness(
                    proc_frame, detection.charuco_corners
                )
                frame_buffer.append((
                    proc_frame.copy(),
                    detection.charuco_corners.copy(),
                    detection.charuco_ids.copy(),
                    detection.num_corners,
                    metrics.copy(),
                    sharpness,
                ))

                # Only enter the lock cycle when the board is
                # momentarily stable. Without this, the user passes
                # THROUGH useful regions while walking toward a new
                # target (e.g. stepping closer) and hears "Hold still"
                # on every transient bin they cross — which is what
                # happened on the last run. The shallow 5-frame /
                # 6 px-variance window returns True as soon as the
                # user briefly pauses, but stays False during active
                # motion like walking or sweeping.
                momentarily_stable = det.is_momentarily_stable(
                    detection.charuco_corners
                )

                # Track how long the user has been holding a useful pose
                # regardless of variance — used by the fallback path below.
                if can_capture and is_useful:
                    if useful_since == 0.0:
                        useful_since = now
                else:
                    useful_since = 0.0

                # Fallback: if the user has been in a useful pose for
                # USEFUL_HOLD_FALLBACK seconds but has never tripped the
                # variance gate (e.g. they're sweeping smoothly), force
                # the lock so we still capture them. The sharpest-frame
                # buffer will discard any motion-blurred candidates.
                useful_held_long = (
                    useful_since > 0.0
                    and (now - useful_since) >= USEFUL_HOLD_FALLBACK
                )

                if can_capture and is_useful and \
                        (momentarily_stable or useful_held_long):
                    if lock_start_time == 0.0:
                        # First frame of a new lock: speak "Hold still"
                        # and start the hold timer. The actual capture
                        # happens once the hold window elapses.
                        lock_start_time = now
                        if not lock_announced:
                            guide.on_lock()
                            lock_announced = True
                            if web_push:
                                web_push("stable",
                                         guide.current_instruction,
                                         tracker.total_samples())
                    elif (now - lock_start_time) >= LOCK_HOLD and \
                            len(frame_buffer) >= 3:
                        # Hold window elapsed — grab the sharpest frame
                        # from the buffer (which now contains frames
                        # captured while the user was holding still).
                        best = max(frame_buffer, key=lambda r: r[5])
                        cap_frame = best[0]
                        cap_corners = best[1]
                        cap_ids = best[2]
                        cap_ncorners = best[3]
                        metrics = best[4]
                        do_capture = True
                        frame_buffer.clear()
                else:
                    # Either the pose stopped being useful, the capture
                    # cooldown kicked in, OR the user is still actively
                    # moving (momentarily_stable == False). In all three
                    # cases we drop any pending lock so the next stable
                    # useful pose gets its own fresh "Hold still"
                    # announcement instead of inheriting a stale one.
                    lock_start_time = 0.0
                    lock_announced = False

            elif args.capture_mode == "timed":
                # Timed mode: capture every N seconds when board is visible.
                interval = args.capture_interval
                if can_capture and (now - last_capture_time) >= interval:
                    do_capture = True

            else:
                # Stable mode (legacy): require low corner variance.
                is_stable = det.check_stability(detection.charuco_corners)
                if is_stable and can_capture and is_useful:
                    do_capture = True

            if do_capture and cap_ncorners >= 6:
                # ── Capture ──────────────────────────────────────────────
                if args.capture_mode == "stable":
                    guide.on_stable()
                    if web_push:
                        web_push("stable", guide.current_instruction,
                                 tracker.total_samples())
                    time.sleep(0.3)

                all_corners.append(cap_corners)
                all_ids.append(cap_ids)
                newly_filled = tracker.update(metrics)
                # Positive feedback: if this capture finally landed a bin
                # the user had been actively chasing (or one we'd given
                # up on), announce the win before the next guidance prompt.
                for _ax, _bi in newly_filled:
                    guide.celebrate_bin(_ax, _bi, args.bins)
                det.reset_stability()
                last_capture_time = time.time()

                # Clear the lock so the next useful pose gets its own
                # "Hold still" announcement rather than re-using this one.
                lock_start_time = 0.0
                lock_announced = False
                useful_since = 0.0

                sector = guide.current_sector or "center"
                orientation = guide.current_orientation or "flat"
                d_score = tracker.diversity_score()
                guide.on_capture(sector, orientation,
                                 diversity_score=d_score)

                if web_push:
                    web_push("captured", guide.current_instruction,
                             tracker.total_samples())

                if sample_frame is None or \
                        tracker.total_samples() == args.min_captures // 2:
                    sample_frame = cap_frame.copy()

                n = tracker.total_samples()
                print(f"  ✓ Captured pose {n:>2}  "
                      f"[sector={sector}, tilt={orientation}, "
                      f"corners={cap_ncorners}, "
                      f"diversity={d_score:.2f}]")

                # ── Completion gate ──────────────────────────────────────
                # Two independent exit conditions:
                #   (A) Graduated: n ≥ min_captures, min-axis coverage is
                #       sufficient, AND a fresh trial calibration's
                #       confidence score ≥ args.confidence_threshold.
                #   (B) Safety: n ≥ args.max_captures (hard cap).
                #
                # Trial calibrations are expensive, so we only run them
                # every args.trial_calibration_every captures past the
                # minimum (and always on the final capture).
                should_try_trial = (
                    n >= args.min_captures and
                    (n - last_trial_n) >= args.trial_calibration_every and
                    tracker.min_axis_coverage() >= args.min_axis_coverage
                )

                finish_run = False

                if should_try_trial:
                    last_trial_n = n
                    print(f"  🔬 Trial calibration at n={n}...")
                    conf, trial_rms, trial_fx, err = run_trial_confidence(
                        all_corners, all_ids, board, (pw, ph),
                        tracker, args.outlier_rms,
                    )
                    if conf is None:
                        print(f"     (trial solver failed: {err})")
                    else:
                        live_confidence = conf["score"]
                        live_rms = trial_rms
                        live_fx = trial_fx
                        print(f"     score={live_confidence:.1f}/100  "
                              f"grade={conf['grade']}  "
                              f"rms={trial_rms:.4f}  "
                              f"fx={trial_fx:.1f}  "
                              f"±{conf['fx_uncertainty_pct_estimate']:.2f}%")
                        if live_confidence >= args.confidence_threshold:
                            finish_run = True
                        else:
                            # Speak the largest point loss so the user knows
                            # what to fix next.
                            worst = conf.get("worst_factors") or []
                            if worst:
                                top = worst[0]["factor"].replace("_", " ")
                                short = (
                                    f"Confidence at {live_confidence:.0f}. "
                                    f"Worst factor: {top}. Keep going."
                                )
                                guide.current_instruction = short
                                if not args.no_voice:
                                    guide._say(short, rate=175)
                                    guide._last_voice_time = time.time()

                if n >= args.max_captures and not finish_run:
                    print(f"  ⚠ Hit --max_captures={args.max_captures} cap — "
                          f"finishing with current data.")
                    finish_run = True

                if finish_run:
                    guide.on_calibration_complete()
                    if web_push:
                        web_push("complete", guide.current_instruction, n)
                    guide.wait_for_speech()
                    running = False

            else:
                # Board visible but not yet captured
                if web_push:
                    web_push("detected", guide.current_instruction,
                             tracker.total_samples())

        else:
            # ── No detection ─────────────────────────────────────────────
            det.corner_history.clear()
            # Board lost — drop any pending lock so the next useful
            # pose is announced fresh.
            lock_start_time = 0.0
            lock_announced = False
            guide.on_no_detection()
            if web_push:
                # Only flash the iPad red if guidance actually switched
                # to the no-detection message (i.e. grace period expired).
                # During brief dropouts keep the iPad on amber/detected.
                elapsed = time.time() - guide._last_detection_time
                if elapsed >= guide.NO_DETECT_GRACE or tracker.total_samples() == 0:
                    web_push("no_detection", guide.current_instruction,
                             tracker.total_samples())
                else:
                    web_push("detected", guide.current_instruction,
                             tracker.total_samples())

        # Periodic guidance: target the most-starved bin by name.
        #
        # We prefer the per-bin guidance over the old generic axis hints
        # because it names the exact physical move ("hold near the right
        # edge") instead of a vague "step sideways". If every bin is
        # already full but the confidence gate hasn't cleared yet, we
        # fall back to the diversity-based hint so the user still hears
        # *something* actionable.
        now = time.time()
        if (not args.no_voice
                and now - last_suggestion_time > suggestion_interval
                and tracker.total_samples() > 3):
            starved = tracker.get_starved_bin()
            if starved is not None:
                ax, bin_idx, _deficit = starved
                spoken = guide.suggest_bin(ax, bin_idx, args.bins)
                # Only count as an "attempt" if we actually spoke the
                # instruction — otherwise the user had no chance to act.
                if spoken is not None:
                    gave_up = tracker.mark_attempted(ax, bin_idx)
                    if gave_up:
                        # One-time acknowledgement so the user knows why
                        # the prompts are about to change target. We wait
                        # briefly for the current instruction to finish
                        # speaking, then deliver axis-specific recovery
                        # advice (not just a generic "skipping").
                        guide.wait_for_speech(timeout=4.0)
                        skip_msg = guide.on_give_up(ax, bin_idx, args.bins)
                        print(f"[INFO] give-up  axis={ax} bin={bin_idx}  "
                              f"skipped_total={len(tracker._skipped_bins)}")
                        guide._last_voice_time = time.time()

                        # Struggle detection: if we've now given up on
                        # several bins, the user is probably stuck with
                        # a bad room layout. Tell them once so they know
                        # to physically reposition.
                        if (len(tracker._skipped_bins) >= 3
                                and not struggle_announced):
                            guide.wait_for_speech(timeout=6.0)
                            guide.on_struggling()
                            struggle_announced = True
                            guide._last_voice_time = time.time()
            else:
                guide.suggest_next_pose(
                    quality_progress=tracker.progress(),
                    diversity_score=tracker.diversity_score(),
                )
            last_suggestion_time = now
            if web_push:
                web_push("detected", guide.current_instruction,
                         tracker.total_samples())

        # Draw HUD
        vis = draw_hud(
            proc_frame, detection, tracker, guide,
            camera_matrix, dist_coeffs, rvec, tvec,
        )

        # Show locally
        cv2.imshow(FEED_WINDOW, vis)
        if pattern_window:
            pattern_window.update()

        # Push to web MJPEG feed
        if ipad_mode:
            web_push_frame(vis)

        # Key handling
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            print("\n[INFO] Quit by user.")
            if web_push:
                web_push("complete", "Quit by user.", tracker.total_samples())
            running = False
        elif key == ord('c'):
            if detection.found and detection.num_corners >= 4:
                metrics = QualityTracker.compute_metrics(
                    detection.charuco_corners, pw, ph
                )
                all_corners.append(detection.charuco_corners)
                all_ids.append(detection.charuco_ids)
                tracker.update(metrics)
                det.reset_stability()
                last_capture_time = time.time()
                n = tracker.total_samples()
                print(f"  ⚡ Force-captured pose {n}")
                if web_push:
                    web_push("captured", f"Force-captured pose {n}", n)
                if sample_frame is None:
                    sample_frame = proc_frame.copy()
        elif key == ord('r'):
            all_corners.clear()
            all_ids.clear()
            frame_buffer.clear()
            tracker = QualityTracker(n_bins=args.bins, min_per_bin=args.min_per_bin)
            guide = VoiceGuidance(
                min_captures=args.min_captures,
                user_facing_camera=args.user_facing_camera,
            )
            if args.no_voice:
                guide._tts = None
            det.reset_stability()
            print("[INFO] Reset — starting over.")
            if web_push:
                web_push("no_detection", "Reset — starting over.", 0)

    # ── 7. Cleanup display ───────────────────────────────────────────────────
    cv2.destroyAllWindows()
    if isinstance(source, ThreadedCapture):
        source.release()
    elif hasattr(source, "release"):
        source.release()

    # ── 8. Run calibration ───────────────────────────────────────────────────
    n = len(all_corners)
    if n < 5:
        print(f"\n[ERROR] Only {n} samples collected — need at least 5.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  CALIBRATING WITH {n} SAMPLES")
    print(f"{'='*60}")

    rms, K, dist, rvecs, tvecs, used_corners, used_ids, health = calibrate_charuco(
        all_corners, all_ids, board, (pw, ph), args.outlier_rms
    )

    errors = compute_per_frame_errors_charuco(
        used_corners, used_ids, board, rvecs, tvecs, K, dist
    )

    # ── 9. Print & export results ────────────────────────────────────────────
    print_results(rms, K, dist, errors)

    # Per-axis bin distribution (post-mortem) and overall confidence score.
    quality_distribution = tracker.as_dict()
    confidence = compute_confidence(
        rms=rms,
        K=K,
        dist=dist,
        health=health,
        tracker_dict=quality_distribution,
        per_frame_errors=errors,
    )
    print_confidence_summary(confidence)

    export_intrinsics_json(
        args.output, K, dist, rms, (pw, ph),
        health=health,
        quality_distribution=quality_distribution,
        confidence=confidence,
    )
    export_calibration_npz(args.output, K, dist, rms, (pw, ph))
    save_verification_image(args.output, sample_frame, K, dist, (pw, ph))

    guide.on_done(rms)
    guide.wait_for_speech()

    print(f"\n✅ Done! Files saved with prefix: {args.output}")


if __name__ == "__main__":
    main()
