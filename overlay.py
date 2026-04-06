"""
Overlay / HUD
=============
Draws detection visualisations, quality bars, 3D axes, guidance text,
and status information on the remote feed window.
"""

import cv2
import numpy as np
from quality_tracker import QualityTracker


# ── Colour palette ────────────────────────────────────────────────────────────
COL_BG       = (20, 20, 20)
COL_TEXT      = (220, 220, 220)
COL_DIM       = (140, 140, 140)
COL_GREEN     = (0, 230, 100)
COL_AMBER     = (0, 200, 255)
COL_RED       = (60, 60, 255)

AXIS_COLORS = {
    "x":    (255, 180, 100),   # warm blue
    "y":    (100, 255, 160),   # green
    "size": (80, 180, 255),    # orange
    "skew": (200, 100, 255),   # purple
}

AXIS_LABELS = {
    "x": "X pos",
    "y": "Y pos",
    "size": "Size",
    "skew": "Skew",
}


def draw_charuco_corners(frame, detection):
    """Draw detected ChArUco corners on the frame."""
    vis = frame.copy()
    if detection.found and detection.charuco_corners is not None:
        cv2.aruco.drawDetectedCornersCharuco(
            vis, detection.charuco_corners, detection.charuco_ids,
            cornerColor=(0, 255, 0)
        )
    if detection.marker_corners is not None and detection.marker_ids is not None:
        cv2.aruco.drawDetectedMarkers(vis, detection.marker_corners, detection.marker_ids)
    return vis


def draw_axis(frame, camera_matrix, dist_coeffs, rvec, tvec, axis_length=0.06):
    """Draw a 3D coordinate axis at the board's origin."""
    if rvec is None or tvec is None:
        return frame
    vis = frame.copy()
    cv2.drawFrameAxes(vis, camera_matrix, dist_coeffs, rvec, tvec, axis_length, 3)
    return vis


def draw_quality_bars(frame, tracker, frame_w, frame_h):
    """Draw the ROS-style quality bar sidebar."""
    vis = frame.copy()
    bar_w = 210

    # Semi-transparent sidebar
    overlay = vis.copy()
    cv2.rectangle(overlay, (frame_w - bar_w, 0), (frame_w, frame_h), COL_BG, -1)
    cv2.addWeighted(overlay, 0.60, vis, 0.40, 0, vis)

    prog = tracker.progress()
    y0 = 30
    row_h = 48
    inner_w = bar_w - 28

    for i, ax in enumerate(QualityTracker.AXES):
        y = y0 + i * row_h
        filled = int(inner_w * prog[ax])
        col = AXIS_COLORS[ax]

        # Label
        cv2.putText(vis, f"{AXIS_LABELS[ax]}  {prog[ax]*100:.0f}%",
                    (frame_w - bar_w + 12, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, COL_TEXT, 1, cv2.LINE_AA)

        # Background track
        cv2.rectangle(vis,
                      (frame_w - bar_w + 12, y + 6),
                      (frame_w - 16, y + 22),
                      (60, 60, 60), -1)

        # Filled portion
        if filled > 0:
            cv2.rectangle(vis,
                          (frame_w - bar_w + 12, y + 6),
                          (frame_w - bar_w + 12 + filled, y + 22),
                          col, -1)

    return vis


def draw_status(frame, tracker, guidance, detection, frame_w, frame_h):
    """Draw status information at the bottom of the sidebar."""
    vis = frame.copy()
    bar_w = 210

    n = tracker.total_samples()
    done = tracker.is_complete()

    # Status text
    if done:
        status = "COMPLETE"
        s_col = COL_GREEN
    elif detection.found:
        status = "DETECTED"
        s_col = COL_GREEN
    else:
        status = "Searching..."
        s_col = COL_AMBER

    # Sample count
    cv2.putText(vis, f"Samples: {n}",
                (frame_w - bar_w + 12, frame_h - 90),
                cv2.FONT_HERSHEY_SIMPLEX, 0.50, COL_TEXT, 1, cv2.LINE_AA)

    # Status
    cv2.putText(vis, status,
                (frame_w - bar_w + 12, frame_h - 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, s_col, 2, cv2.LINE_AA)

    # Sector & orientation
    if guidance.current_sector:
        sector_text = f"Sector: {guidance.current_sector}"
        cv2.putText(vis, sector_text,
                    (frame_w - bar_w + 12, frame_h - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, COL_DIM, 1, cv2.LINE_AA)
    if guidance.current_orientation:
        orient_text = f"Tilt: {guidance.current_orientation}"
        cv2.putText(vis, orient_text,
                    (frame_w - bar_w + 12, frame_h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, COL_DIM, 1, cv2.LINE_AA)

    return vis


def draw_instruction(frame, instruction, frame_w):
    """Draw the current voice instruction as a top banner."""
    vis = frame.copy()
    # Top banner
    banner_h = 40
    overlay = vis.copy()
    cv2.rectangle(overlay, (0, 0), (frame_w, banner_h), COL_BG, -1)
    cv2.addWeighted(overlay, 0.70, vis, 0.30, 0, vis)

    cv2.putText(vis, instruction,
                (12, 27),
                cv2.FONT_HERSHEY_SIMPLEX, 0.52, COL_AMBER, 1, cv2.LINE_AA)
    return vis


def draw_hud(frame, detection, tracker, guidance, camera_matrix=None, dist_coeffs=None,
             rvec=None, tvec=None):
    """
    Composite all HUD elements onto the frame.
    Returns the annotated frame.
    """
    h, w = frame.shape[:2]
    vis = frame.copy()

    # 1. Draw detected corners
    vis = draw_charuco_corners(vis, detection)

    # 2. Draw 3D axis if we have pose
    if rvec is not None and camera_matrix is not None:
        vis = draw_axis(vis, camera_matrix, dist_coeffs, rvec, tvec)

    # 3. Quality bars
    vis = draw_quality_bars(vis, tracker, w, h)

    # 4. Status
    vis = draw_status(vis, tracker, guidance, detection, w, h)

    # 5. Instruction banner
    vis = draw_instruction(vis, guidance.current_instruction, w)

    return vis
