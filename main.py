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

    python main.py --rtsp rtsp://… --ipad --ipad_model ipad-pro-11
    python main.py --rtsp rtsp://… --ipad --board_width_mm 165

Single-screen mode  (original)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    python main.py --rtsp rtsp://…
    python main.py --video test_video.mp4
    python main.py --camera 0
"""

import argparse
import cv2
import numpy as np
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
    compute_device_board_width,
    list_known_devices,
    KNOWN_DEVICES,
)
from exporter import (
    export_intrinsics_json,
    export_calibration_npz,
    save_verification_image,
    print_results,
)


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="MDCT-Proto: Dynamic Calibration Target",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  # iPad auto-capture (recommended — just wave the iPad slowly)\n"
            "  python main.py --rtsp rtsp://… --ipad --ipad_model ipad-pro-11\n\n"
            "  # Timed capture — one capture every 3 seconds\n"
            "  python main.py --rtsp rtsp://… --ipad --ipad_model ipad-pro-11 "
            "--capture_mode timed --capture_interval 3\n\n"
            "  # Legacy stable mode (requires holding iPad still)\n"
            "  python main.py --rtsp rtsp://… --ipad --ipad_model ipad-pro-11 "
            "--capture_mode stable\n\n"
            "  # Local-screen mode\n"
            "  python main.py --rtsp rtsp://… --board_width_mm 280\n\n"
            "  # List known iPad models\n"
            "  python main.py --list_devices\n"
        ),
    )

    # Input source (mutually exclusive)
    src = parser.add_mutually_exclusive_group(required="--list_devices" not in sys.argv)
    src.add_argument("--rtsp",   type=str, help="RTSP stream URL")
    src.add_argument("--video",  type=str, help="Path to a local video file")
    src.add_argument("--camera", type=int, help="Webcam device index")

    # ── iPad / two-device mode ───────────────────────────────────────────────
    parser.add_argument(
        "--ipad", action="store_true",
        help="Serve the ChArUco pattern to an iPad/tablet via web "
             "(enables two-device mode with visual feedback)",
    )
    parser.add_argument(
        "--ipad_model", type=str, default=None,
        metavar="MODEL",
        help="Known iPad/tablet model for auto board-width computation "
             "(e.g. ipad-pro-11).  Use --list_devices to see options.",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Web-server port for iPad mode (default: 8080)",
    )
    parser.add_argument(
        "--list_devices", action="store_true",
        help="Print known iPad/tablet models and exit",
    )

    # Board display
    parser.add_argument(
        "--board_width_mm", type=float, default=0,
        help="Physical width of the displayed board pattern (mm). "
             "Auto-detected from --ipad_model or Mac screen if omitted.",
    )
    parser.add_argument(
        "--display_width", type=int, default=0,
        help="Pixel width of the rendered ChArUco image "
             "(default: 1200 for iPad, 900 for local)",
    )

    # Calibration parameters
    parser.add_argument("--min_captures", type=int, default=20,
                        help="Minimum number of pose captures (default: 20)")
    parser.add_argument("--bins", type=int, default=5,
                        help="Number of bins per quality axis (default: 5)")
    parser.add_argument("--min_per_bin", type=int, default=2,
                        help="Samples required per bin (default: 2)")
    parser.add_argument("--outlier_rms", type=float, default=0.5,
                        help="Per-frame RMS threshold for outlier rejection (default: 0.5)")
    parser.add_argument("--diversity_threshold", type=float, default=0.35,
                        help="Diversity score (0-1) needed for completion (default: 0.35). "
                             "Lower = easier to finish, higher = more coverage required.")

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

    return parser.parse_args()


# ── Main calibration loop ───────────────────────────────────────────────────

def main():
    args = parse_args()

    # ── --list_devices shortcut ──────────────────────────────────────────────
    if args.list_devices:
        list_known_devices()
        return

    # ── Defaults that depend on mode ─────────────────────────────────────────
    ipad_mode = args.ipad or args.ipad_model is not None
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
        print(f"[INFO] Physical board width: {args.board_width_mm} mm (user-specified)")
    elif args.ipad_model:
        try:
            args.board_width_mm = compute_device_board_width(
                args.ipad_model, args.display_width
            )
            dev = KNOWN_DEVICES[args.ipad_model]
            print(f"[INFO] iPad model: {dev['name']}")
            print(f"[INFO] Physical board width: {args.board_width_mm:.1f} mm "
                  f"(auto-computed for {args.ipad_model}, portrait)")
        except (ValueError, KeyError) as e:
            print(f"[ERROR] {e}")
            list_known_devices()
            sys.exit(1)
    elif not ipad_mode:
        # Try macOS auto-detection (only useful when pattern is on local screen)
        print_screen_info()
        board_img = render_board_image(board, args.display_width)
        detected_mm = auto_detect_board_width(args.display_width, board_img)
        if detected_mm:
            args.board_width_mm = detected_mm
            print(f"[INFO] Physical board width: {args.board_width_mm:.1f} mm "
                  f"(auto-detected)")
        else:
            print("[WARN] Could not auto-detect screen dimensions.")
            print("       Provide --board_width_mm for accurate calibration.")
    else:
        print("[WARN] No --board_width_mm or --ipad_model specified.")
        print("       Provide one for accurate real-world calibration.")
        print("       Proceeding with default board units.")

    # ── 4. Open feed ─────────────────────────────────────────────────────────
    source_type, source, frame_size = open_source(
        rtsp=args.rtsp, video=args.video, camera=args.camera
    )
    frame_w, frame_h = frame_size
    print(f"[INFO] Frame size: {frame_w}x{frame_h}")

    # ── 5. Initialise modules ────────────────────────────────────────────────
    det = CharucoDetector(
        board, aruco_dict,
        stability_frames=args.stability_frames,
        stability_threshold=args.stability_threshold,
    )
    tracker = QualityTracker(n_bins=args.bins, min_per_bin=args.min_per_bin)
    guide = VoiceGuidance(min_captures=args.min_captures)

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

    print(f"\n[INFO] Calibration loop started. Collect {args.min_captures} poses.")
    print(f"[INFO] Capture mode: {args.capture_mode}")
    print(f"[INFO] Quality bins: {args.bins} bins/axis x min {args.min_per_bin} "
          f"samples/bin")
    print("[INFO] Press 'q' to quit, 'c' to force-capture, 'r' to reset\n")

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

    while running:
        ret, frame = read_frame(source_type, source)
        if not ret or frame is None:
            if source_type == "video":
                print("\n[INFO] End of video.")
                break
            time.sleep(0.01)
            continue

        frame_count += 1
        proc_frame = resize_frame(frame)
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
                # appears, pick the sharpest frame from the buffer.
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

                if can_capture and is_useful and len(frame_buffer) >= 3:
                    # Pick the sharpest frame from the buffer
                    best = max(frame_buffer, key=lambda r: r[5])
                    cap_frame = best[0]
                    cap_corners = best[1]
                    cap_ids = best[2]
                    cap_ncorners = best[3]
                    metrics = best[4]
                    do_capture = True
                    frame_buffer.clear()

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
                tracker.update(metrics)
                det.reset_stability()
                last_capture_time = time.time()

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

                # Check completion (diversity-based, not rigid grid)
                if tracker.is_good_enough(
                    min_samples=args.min_captures,
                    score_threshold=args.diversity_threshold,
                ):
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

        # Periodic guidance (stops once diversity is good enough)
        now = time.time()
        if (not args.no_voice
                and now - last_suggestion_time > suggestion_interval
                and tracker.total_samples() > 3
                and not tracker.is_good_enough(
                    min_samples=args.min_captures,
                    score_threshold=args.diversity_threshold)):
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
            guide = VoiceGuidance(min_captures=args.min_captures)
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

    rms, K, dist, rvecs, tvecs, used_corners, used_ids = calibrate_charuco(
        all_corners, all_ids, board, (pw, ph), args.outlier_rms
    )

    errors = compute_per_frame_errors_charuco(
        used_corners, used_ids, board, rvecs, tvecs, K, dist
    )

    # ── 9. Print & export results ────────────────────────────────────────────
    print_results(rms, K, dist, errors)
    export_intrinsics_json(args.output, K, dist, rms, (pw, ph))
    export_calibration_npz(args.output, K, dist, rms, (pw, ph))
    save_verification_image(args.output, sample_frame, K, dist, (pw, ph))

    guide.on_done(rms)
    guide.wait_for_speech()

    print(f"\n✅ Done! Files saved with prefix: {args.output}")


if __name__ == "__main__":
    main()
