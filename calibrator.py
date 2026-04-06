"""
Calibrator
==========
Two-pass calibration with outlier rejection.
Uses cv2.calibrateCamera with CharucoBoard.matchImagePoints()
(compatible with OpenCV 4.13+).
"""

import cv2
import numpy as np


def _charuco_to_obj_img_points(all_corners, all_ids, board):
    """
    Convert per-frame ChArUco corners + ids into matched object/image
    point lists suitable for cv2.calibrateCamera.
    """
    obj_points = []
    img_points = []
    for corners, ids in zip(all_corners, all_ids):
        obj_pts, img_pts = board.matchImagePoints(corners, ids)
        if obj_pts is not None and len(obj_pts) >= 4:
            obj_points.append(obj_pts)
            img_points.append(img_pts)
    return obj_points, img_points


def calibrate_charuco(all_corners, all_ids, board, frame_size, outlier_rms=0.5):
    """
    Two-pass ChArUco camera calibration.

    Pass 1: calibrate on all accepted frames.
    Pass 2: drop frames with per-frame RMS > outlier_rms, recalibrate.

    Returns:
        rms        : float  -- final RMS re-projection error
        K          : (3,3)  -- camera intrinsic matrix
        dist       : (1,5)  -- distortion coefficients
        rvecs      : list   -- rotation vectors
        tvecs      : list   -- translation vectors
        used_corners: list  -- corners used (after outlier removal)
        used_ids   : list   -- ids used (after outlier removal)
    """
    n = len(all_corners)
    print(f"\n[INFO] Pass 1: calibrating with {n} frames ...")

    obj_points, img_points = _charuco_to_obj_img_points(
        all_corners, all_ids, board
    )

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, frame_size, None, None
    )
    print(f"       RMS = {rms:.5f} px")

    # Per-frame errors
    frame_errors = compute_per_frame_errors_charuco(
        all_corners, all_ids, board, rvecs, tvecs, K, dist
    )

    # Identify outliers
    outliers = [i for i, e in enumerate(frame_errors) if e > outlier_rms]

    if outliers:
        print(f"\n[INFO] Dropping {len(outliers)} outlier frame(s) "
              f"(per-frame RMS > {outlier_rms} px)")

        clean_corners = [c for i, c in enumerate(all_corners) if i not in set(outliers)]
        clean_ids = [d for i, d in enumerate(all_ids) if i not in set(outliers)]

        if len(clean_corners) < 10:
            print("[WARN] Too few frames remain after outlier removal -- keeping all.")
            clean_corners, clean_ids = all_corners, all_ids
        else:
            print(f"[INFO] Pass 2: recalibrating with {len(clean_corners)} frames ...")
            obj_points2, img_points2 = _charuco_to_obj_img_points(
                clean_corners, clean_ids, board
            )
            rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
                obj_points2, img_points2, frame_size, None, None
            )
            print(f"       RMS = {rms:.5f} px")
            all_corners, all_ids = clean_corners, clean_ids
    else:
        print(f"[INFO] No outliers found above {outlier_rms} px -- keeping all frames.")

    return rms, K, dist, rvecs, tvecs, all_corners, all_ids


def compute_per_frame_errors_charuco(corners_list, ids_list, board, rvecs, tvecs, K, dist):
    """Compute per-frame reprojection error for ChArUco calibration."""
    errors = []
    obj_points_all = board.getChessboardCorners()

    for corners, ids, rvec, tvec in zip(corners_list, ids_list, rvecs, tvecs):
        # Get the 3D object points for detected corner IDs
        obj_pts = np.array([obj_points_all[i] for i in ids.flatten()], dtype=np.float32)
        img_pts = corners.reshape(-1, 2)

        # Project object points
        proj, _ = cv2.projectPoints(obj_pts, rvec, tvec, K, dist)
        proj = proj.reshape(-1, 2)

        # Compute error
        error = np.sqrt(np.mean(np.sum((img_pts - proj) ** 2, axis=1)))
        errors.append(error)

    return errors
