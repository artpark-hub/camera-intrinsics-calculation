"""
Vision & Pose Estimation
========================
ChArUco board detection, pose estimation, and stability checking.
Uses OpenCV 4.8+ CharucoDetector API.
"""

import cv2
import numpy as np
from collections import deque


class CharucoDetector:
    """Detect a ChArUco board in a frame and estimate its 3D pose."""

    def __init__(self, board, aruco_dict, stability_frames=30, stability_threshold=2.0):
        """
        board              : cv2.aruco.CharucoBoard
        aruco_dict         : cv2.aruco.Dictionary
        stability_frames   : number of frames to track for stability check
        stability_threshold: max variance in corner positions (pixels) to accept
        """
        self.board = board
        self.aruco_dict = aruco_dict

        # Detector parameters
        detector_params = cv2.aruco.DetectorParameters()
        detector_params.adaptiveThreshWinSizeMin = 3
        detector_params.adaptiveThreshWinSizeMax = 23
        detector_params.adaptiveThreshWinSizeStep = 10
        detector_params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX

        self.aruco_detector = cv2.aruco.ArucoDetector(aruco_dict, detector_params)
        self.charuco_detector = cv2.aruco.CharucoDetector(board)

        # Stability tracking
        self.stability_frames = stability_frames
        self.stability_threshold = stability_threshold
        self.corner_history = deque(maxlen=stability_frames)

    def detect(self, frame):
        """
        Detect ChArUco board in the given frame.

        Returns:
            DetectionResult with fields:
              .found        : bool — any corners detected
              .charuco_corners : (N,1,2) array of detected charuco corner positions
              .charuco_ids  : (N,1) array of corner IDs
              .marker_corners: list of marker corner arrays
              .marker_ids   : array of marker IDs
              .num_corners  : int — number of charuco corners detected
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame

        result = DetectionResult()

        # Detect ArUco markers
        marker_corners, marker_ids, rejected = self.aruco_detector.detectMarkers(gray)

        if marker_ids is None or len(marker_ids) < 1:
            return result

        result.marker_corners = marker_corners
        result.marker_ids = marker_ids

        # Interpolate ChArUco corners from ArUco markers
        charuco_corners, charuco_ids, _, _ = self.charuco_detector.detectBoard(gray)

        if charuco_ids is None or len(charuco_ids) < 4:
            return result

        result.found = True
        result.charuco_corners = charuco_corners
        result.charuco_ids = charuco_ids
        result.num_corners = len(charuco_ids)

        return result

    def estimate_pose(self, frame, detection, camera_matrix, dist_coeffs):
        """
        Estimate the 3D pose of the detected board using
        board.matchImagePoints() + cv2.solvePnP().

        Returns:
            (success, rvec, tvec) or (False, None, None)
        """
        if not detection.found or detection.num_corners < 6:
            return False, None, None

        # Map detected corner IDs → 3-D object points + 2-D image points
        obj_pts, img_pts = self.board.matchImagePoints(
            detection.charuco_corners, detection.charuco_ids
        )
        if obj_pts is None or len(obj_pts) < 6:
            return False, None, None

        success, rvec, tvec = cv2.solvePnP(
            obj_pts, img_pts, camera_matrix, dist_coeffs
        )
        if success:
            return True, rvec, tvec
        return False, None, None

    def check_stability(self, corners):
        """
        Track corner positions over recent frames and return True
        if the board is stable enough to capture.
        """
        if corners is None:
            self.corner_history.clear()
            return False

        self.corner_history.append(corners.copy())

        if len(self.corner_history) < min(5, self.stability_frames):
            return False

        # Compute variance of corner positions across recent frames.
        # Use mean position of all corners as a simple metric.
        # Only look at the most recent frames (handles hand tremor better
        # than requiring the entire history to be stable).
        window = list(self.corner_history)[-self.stability_frames:]
        positions = []
        for hist_corners in window:
            mean_pos = hist_corners.reshape(-1, 2).mean(axis=0)
            positions.append(mean_pos)

        positions = np.array(positions)
        variance = np.var(positions, axis=0).sum()

        return variance < self.stability_threshold

    def reset_stability(self):
        """Clear the stability history after a capture."""
        self.corner_history.clear()

    def is_momentarily_stable(self, corners,
                              window: int = 3,
                              threshold: float = 15.0) -> bool:
        """
        Lightweight "is the board roughly still *right now*" check for
        auto-mode lock gating.

        Unlike `check_stability`, which looks at `self.stability_frames`
        of history (~1 s at 30 fps) and expects near-zero variance, this
        check looks at only the most recent `window` frames (~150 ms)
        and allows a larger pixel variance. The intent is different:
          - `check_stability`  -> "board is rock-solid, safe to capture"
          - `is_momentarily_stable` -> "user has briefly paused, it is
            fair to say 'Hold still' and start the lock countdown"

        Returns False while the user is actively walking or sweeping,
        True once they pause — even briefly. This is what prevents the
        "Hold still" prompt from firing while the user is still moving
        THROUGH a useful region on the way to somewhere else.
        """
        if corners is None:
            self.corner_history.clear()
            return False

        self.corner_history.append(corners.copy())

        if len(self.corner_history) < window:
            return False

        recent = list(self.corner_history)[-window:]
        positions = np.array([
            c.reshape(-1, 2).mean(axis=0) for c in recent
        ])
        variance = float(np.var(positions, axis=0).sum())
        return variance < threshold

    @staticmethod
    def board_sharpness(frame, corners):
        """
        Compute a sharpness score for the board region of the frame.

        Uses the variance of the Laplacian — higher = sharper.
        Only evaluates the bounding-box region around detected corners
        so background blur doesn't affect the score.
        """
        pts = corners.reshape(-1, 2)
        x_min, y_min = pts.min(axis=0).astype(int)
        x_max, y_max = pts.max(axis=0).astype(int)

        h, w = frame.shape[:2]
        pad = 10
        x_min = max(0, x_min - pad)
        y_min = max(0, y_min - pad)
        x_max = min(w, x_max + pad)
        y_max = min(h, y_max + pad)

        roi = frame[y_min:y_max, x_min:x_max]
        if roi.size == 0:
            return 0.0

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class DetectionResult:
    """Container for detection results."""
    def __init__(self):
        self.found = False
        self.charuco_corners = None
        self.charuco_ids = None
        self.marker_corners = None
        self.marker_ids = None
        self.num_corners = 0
