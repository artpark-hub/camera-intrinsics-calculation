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


def calibrate_charuco(
    all_corners, all_ids, board, frame_size,
    outlier_rms=0.5,
    fix_k3=True,
    fix_aspect_ratio=True,
    quiet=False,
):
    """
    Two-pass ChArUco camera calibration with constrained model.

    Pass 1: calibrate on all accepted frames.
    Pass 2: drop frames with per-frame RMS > outlier_rms, recalibrate.

    Model constraints (defaults match what is physically reasonable for
    a hand-held tablet target on a CMOS sensor):
        fix_k3=True            -- k3 is held at 0. With limited corner
                                  coverage k3 is the first parameter to
                                  go wild and produce unphysical
                                  distortion (|k3| in the hundreds).
        fix_aspect_ratio=True  -- forces fx == fy. Square-pixel sensors
                                  have fx = fy by physics; allowing them
                                  to drift apart is almost always the
                                  optimizer compensating for an
                                  underconstrained problem.

    Pass either flag as False if you genuinely have a non-square-pixel
    sensor or full image-corner coverage and want to estimate k3.

    Returns:
        rms          : final RMS re-projection error (px)
        K            : (3,3) camera intrinsic matrix
        dist         : (1,5) distortion coefficients [k1, k2, p1, p2, k3]
        rvecs, tvecs : per-frame extrinsics
        used_corners : corners kept after outlier removal
        used_ids     : ids kept after outlier removal
        health       : dict of diagnostic flags (see _calibration_health)
    """
    flags = 0
    if fix_k3:
        flags |= cv2.CALIB_FIX_K3
    if fix_aspect_ratio:
        flags |= cv2.CALIB_FIX_ASPECT_RATIO  # requires an initial K with fx=fy

    # When CALIB_FIX_ASPECT_RATIO is set, OpenCV needs an initial guess for K
    # with the desired aspect ratio. A reasonable seed is fx = fy = max(w, h).
    init_K = None
    if fix_aspect_ratio:
        fw, fh = frame_size
        f0 = float(max(fw, fh))
        init_K = np.array([[f0, 0, fw / 2.0],
                           [0, f0, fh / 2.0],
                           [0, 0,     1.0  ]], dtype=np.float64)

    n = len(all_corners)
    constraints = []
    if fix_k3:
        constraints.append("k3=0")
    if fix_aspect_ratio:
        constraints.append("fx=fy")
    cstr = (" [" + ", ".join(constraints) + "]") if constraints else ""
    if not quiet:
        print(f"\n[INFO] Pass 1: calibrating with {n} frames{cstr} ...")

    obj_points, img_points = _charuco_to_obj_img_points(
        all_corners, all_ids, board
    )

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, frame_size,
        init_K, None,
        flags=flags,
    )
    if not quiet:
        print(f"       RMS = {rms:.5f} px")

    # Per-frame errors
    frame_errors = compute_per_frame_errors_charuco(
        all_corners, all_ids, board, rvecs, tvecs, K, dist
    )

    # Identify outliers
    outliers = [i for i, e in enumerate(frame_errors) if e > outlier_rms]

    if outliers:
        if not quiet:
            print(f"\n[INFO] Dropping {len(outliers)} outlier frame(s) "
                  f"(per-frame RMS > {outlier_rms} px)")

        clean_corners = [c for i, c in enumerate(all_corners) if i not in set(outliers)]
        clean_ids = [d for i, d in enumerate(all_ids) if i not in set(outliers)]

        if len(clean_corners) < 10:
            if not quiet:
                print("[WARN] Too few frames remain after outlier removal -- keeping all.")
            clean_corners, clean_ids = all_corners, all_ids
        else:
            if not quiet:
                print(f"[INFO] Pass 2: recalibrating with {len(clean_corners)} frames ...")
            obj_points2, img_points2 = _charuco_to_obj_img_points(
                clean_corners, clean_ids, board
            )
            rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
                obj_points2, img_points2, frame_size,
                init_K, None,
                flags=flags,
            )
            if not quiet:
                print(f"       RMS = {rms:.5f} px")
            all_corners, all_ids = clean_corners, clean_ids
    else:
        if not quiet:
            print(f"[INFO] No outliers found above {outlier_rms} px -- keeping all frames.")

    health = _calibration_health(K, dist, frame_size, all_corners, rms)
    if not quiet:
        _print_health(health)

    return rms, K, dist, rvecs, tvecs, all_corners, all_ids, health


def _calibration_health(K, dist, frame_size, all_corners, rms):
    """
    Sanity-check the calibration result and return a dict of flags.

    These checks catch the failure modes that produce a low RMS
    'looks great!' result that is actually garbage:

      - principal point far from image center
      - fx and fy disagree (non-square pixel)
      - distortion coefficients in unphysical ranges
      - image-corner coverage of the captured ChArUco corners is too
        low to constrain distortion
      - RMS suspiciously low (overfitting)
    """
    fw, fh = frame_size
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    d = dist.flatten()
    k1 = float(d[0]) if len(d) > 0 else 0.0
    k2 = float(d[1]) if len(d) > 1 else 0.0
    p1 = float(d[2]) if len(d) > 2 else 0.0
    p2 = float(d[3]) if len(d) > 3 else 0.0
    k3 = float(d[4]) if len(d) > 4 else 0.0

    # Aggregate every detected ChArUco corner across every frame, then
    # measure how close the union gets to each image edge. We use the
    # min normalized distance to any edge as the "corner coverage" score.
    all_pts = []
    for c in all_corners:
        if c is None:
            continue
        all_pts.append(c.reshape(-1, 2))
    if all_pts:
        pts = np.concatenate(all_pts, axis=0)
        x_min, x_max = float(pts[:, 0].min()), float(pts[:, 0].max())
        y_min, y_max = float(pts[:, 1].min()), float(pts[:, 1].max())
        # Distance of the closest captured corner to its respective edge,
        # normalized by frame dimension. 0 = touches the edge, 0.5 = stuck
        # in the dead center.
        edge_gap_x = min(x_min, fw - x_max) / fw
        edge_gap_y = min(y_min, fh - y_max) / fh
        edge_gap = max(edge_gap_x, edge_gap_y)
    else:
        edge_gap = 0.5

    aspect_err = abs(fx - fy) / max(fx, fy) if max(fx, fy) > 0 else 0.0
    cx_off = abs(cx - fw / 2.0) / fw
    cy_off = abs(cy - fh / 2.0) / fh

    health = {
        "rms_px": rms,
        "fx": fx, "fy": fy, "cx": cx, "cy": cy,
        "k1": k1, "k2": k2, "k3": k3, "p1": p1, "p2": p2,
        "aspect_err": aspect_err,
        "principal_point_offset_x_frac": cx_off,
        "principal_point_offset_y_frac": cy_off,
        "edge_gap": edge_gap,         # 0 = corners reach the image edge
        "warnings": [],
    }

    if rms < 0.10:
        health["warnings"].append(
            f"RMS = {rms:.3f} px is suspiciously LOW (< 0.10). This usually "
            "indicates the model overfit a small set of corners in a small "
            "region of the frame. RMS on captured frames is meaningless if "
            "the captures don't span the working domain."
        )
    if rms > 1.0:
        health["warnings"].append(
            f"RMS = {rms:.3f} px is high (> 1.0). Calibration is unreliable; "
            "consider rejecting outliers more aggressively or recapturing."
        )
    if aspect_err > 0.01:
        health["warnings"].append(
            f"fx and fy differ by {aspect_err*100:.1f}% (> 1%). Real CMOS "
            "sensors have square pixels (fx == fy). The asymmetry suggests "
            "the optimizer is compensating for missing observations. "
            "Consider re-running with fix_aspect_ratio=True (the default)."
        )
    if cx_off > 0.10 or cy_off > 0.10:
        health["warnings"].append(
            f"Principal point is {cx_off*100:.1f}% / {cy_off*100:.1f}% off "
            "the image center. For most lenses it should be within ~5% of "
            "center; large offsets indicate an underconstrained solve."
        )
    if abs(k1) > 1.5:
        health["warnings"].append(
            f"|k1| = {abs(k1):.2f} is unusually large (> 1.5). Real lenses "
            "rarely exceed |k1| ≈ 0.6. This often happens when the target "
            "never reaches the image edges/corners."
        )
    if abs(k2) > 5.0:
        health["warnings"].append(
            f"|k2| = {abs(k2):.2f} is unphysical (> 5.0). The distortion "
            "model is fitting noise."
        )
    if abs(k3) > 1.0:
        health["warnings"].append(
            f"|k3| = {abs(k3):.2f} is unphysical (> 1.0). Re-run with "
            "fix_k3=True (the default) to hold k3 at 0."
        )
    if edge_gap > 0.15:
        health["warnings"].append(
            f"Captured ChArUco corners never come closer than "
            f"{edge_gap*100:.0f}% of the frame to the nearest edge. "
            "Distortion (k1, k2) is poorly constrained because all "
            "observations are clustered near the image center. "
            "MOVE THE TARGET TO THE EDGES AND CORNERS OF THE FRAME, "
            "or use a larger pattern, or get closer to the camera."
        )
    return health


def _print_health(health):
    print("\n" + "─" * 60)
    print("  CALIBRATION HEALTH CHECK")
    print("─" * 60)
    print(f"  fx, fy            : {health['fx']:.2f}, {health['fy']:.2f}  "
          f"(asymmetry {health['aspect_err']*100:.2f}%)")
    print(f"  cx, cy            : {health['cx']:.2f}, {health['cy']:.2f}")
    print(f"  k1, k2, k3        : {health['k1']:.4f}, {health['k2']:.4f}, "
          f"{health['k3']:.4f}")
    print(f"  p1, p2            : {health['p1']:.4f}, {health['p2']:.4f}")
    print(f"  RMS reproj. error : {health['rms_px']:.4f} px")
    print(f"  Edge-gap (min)    : {health['edge_gap']*100:.1f}%  "
          f"(0% = corners touch image edge — ideal for distortion)")
    if health["warnings"]:
        print(f"\n  ⚠  {len(health['warnings'])} WARNING(S):")
        for i, w in enumerate(health["warnings"], 1):
            print(f"     {i}. {w}")
    else:
        print("\n  ✓  All health checks passed.")
    print("─" * 60)


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
