"""
Data Exporter
=============
Exports calibration results to JSON and NPZ formats,
and generates a verification image.
"""

import cv2
import json
import numpy as np
from pathlib import Path


def export_intrinsics_json(output_path, K, dist, rms, frame_size):
    """
    Export camera intrinsics to a JSON file.

    K        : (3,3) intrinsic matrix
    dist     : (1,5) distortion coefficients
    rms      : float reprojection error
    frame_size: (width, height)
    """
    data = {
        "camera_intrinsics": {
            "focal_length": {
                "fx": float(K[0, 0]),
                "fy": float(K[1, 1]),
            },
            "principal_point": {
                "cx": float(K[0, 2]),
                "cy": float(K[1, 2]),
            },
            "intrinsic_matrix": K.tolist(),
        },
        "distortion_coefficients": {
            "k1": float(dist[0, 0]),
            "k2": float(dist[0, 1]),
            "p1": float(dist[0, 2]),
            "p2": float(dist[0, 3]),
            "k3": float(dist[0, 4]),
            "raw": dist.flatten().tolist(),
        },
        "calibration_quality": {
            "rms_reprojection_error_px": float(rms),
            "quality": "excellent" if rms < 0.5 else "good" if rms < 1.0 else "poor",
        },
        "frame_size": {
            "width": frame_size[0],
            "height": frame_size[1],
        },
    }

    path = Path(output_path)
    json_path = path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  📄 Saved intrinsics → {json_path}")
    return str(json_path)


def export_calibration_npz(output_path, K, dist, rms, frame_size):
    """Export calibration data to NPZ format (for programmatic use)."""
    path = Path(output_path)
    npz_path = path.with_suffix(".npz")
    np.savez(
        npz_path,
        camera_matrix=K,
        dist_coeffs=dist,
        rms_error=rms,
        frame_size=np.array(frame_size),
    )
    print(f"  💾 Saved calibration → {npz_path}")
    print(f'\n    Load:  data = np.load("{npz_path}")')
    print('           K = data["camera_matrix"];  dist = data["dist_coeffs"]')
    return str(npz_path)


def save_verification_image(output_path, sample_frame, K, dist, frame_size):
    """
    Save a 3-panel verification image:
      Original  |  Undistorted (α=0)  |  Undistorted (α=1) + ROI
    """
    if sample_frame is None:
        print("[WARN] No sample frame — skipping verification image.")
        return

    sh, sw = sample_frame.shape[:2]

    # Undistort crop (alpha=0)
    undist_crop = cv2.undistort(sample_frame, K, dist)

    # Undistort full (alpha=1) + ROI rectangle
    new_K, roi = cv2.getOptimalNewCameraMatrix(K, dist, (sw, sh), alpha=1)
    mapx, mapy = cv2.initUndistortRectifyMap(K, dist, None, new_K, (sw, sh), cv2.CV_32FC1)
    undist_full = cv2.remap(sample_frame, mapx, mapy, cv2.INTER_LINEAR)
    x, y, rw, rh = roi
    if rw > 0 and rh > 0:
        cv2.rectangle(undist_full, (x, y), (x + rw, y + rh), (0, 255, 0), 2)

    def label(img, title, sub=""):
        out = img.copy()
        cv2.rectangle(out, (0, 0), (sw, 44), (20, 20, 20), -1)
        cv2.putText(out, title, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        if sub:
            cv2.putText(out, sub, (8, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (180, 180, 180), 1)
        return out

    div = np.full((sh, 6, 3), 70, dtype=np.uint8)
    strip = np.hstack([
        label(sample_frame, "ORIGINAL (distorted)"),
        div,
        label(undist_crop, "UNDISTORTED  alpha=0", "valid pixels, cropped FOV"),
        div,
        label(undist_full, "UNDISTORTED  alpha=1", "all pixels  |  green = valid ROI"),
    ])

    # Info banner
    bh = 72
    banner = np.zeros((bh, strip.shape[1], 3), dtype=np.uint8)
    lines = [
        f"fx={K[0,0]:.2f}  fy={K[1,1]:.2f}  cx={K[0,2]:.2f}  cy={K[1,2]:.2f}",
        f"k1={dist[0,0]:.5f}  k2={dist[0,1]:.5f}  "
        f"p1={dist[0,2]:.5f}  p2={dist[0,3]:.5f}  k3={dist[0,4]:.5f}",
    ]
    for i, ln in enumerate(lines):
        cv2.putText(banner, ln, (10, 22 + i * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 230, 255), 1)

    out_img = str(Path(output_path).with_suffix("")) + "_verification.jpg"
    cv2.imwrite(out_img, np.vstack([strip, banner]))
    print(f"\n  🖼️  Verification image → {out_img}")


def print_results(rms, K, dist, errors=None):
    """Print formatted calibration results to console."""
    print("\n" + "=" * 60)
    print("  CALIBRATION RESULTS")
    print("=" * 60)
    print(f"\n📷  Intrinsic Matrix (K):")
    print(f"    fx = {K[0,0]:.4f} px    fy = {K[1,1]:.4f} px")
    print(f"    cx = {K[0,2]:.4f} px    cy = {K[1,2]:.4f} px")
    print(f"\n{K}\n")

    lbls = ["k1 (radial)", "k2 (radial)", "p1 (tangential)", "p2 (tangential)", "k3 (radial)"]
    print("  Distortion Coefficients:")
    for l, v in zip(lbls, dist.flatten()):
        print(f"    {l:22s} = {v: .8f}")
    print(f"\n    Raw: {dist.flatten()}\n")

    print(f"  RMS Re-projection Error : {rms:.5f} px")
    q = " Excellent" if rms < 0.5 else " Good" if rms < 1.0 else "  Poor — recalibrate"
    print(f"    Quality                : {q}\n")

    if errors:
        print("  Per-frame errors:")
        for i, e in enumerate(errors):
            bar = "█" * min(int(e * 30), 40)
            print(f"    [{i+1:>3}] {e:.4f} px  {bar}")
        print(f"\n    Mean={np.mean(errors):.4f}  Std={np.std(errors):.4f}  "
              f"Max={np.max(errors):.4f}")

    print("=" * 60)
