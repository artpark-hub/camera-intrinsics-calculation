"""
Pattern Display Engine
======================
Generates and displays a ChArUco calibration board.

*  Local mode: borderless, always-on-top OpenCV window on the laptop.
*  iPad mode:  the board is rendered and served as a PNG by the web
   server — no local window needed.
"""

import cv2
import numpy as np


# ── Board parameters (defaults for the original iPad 5×7 pattern) ──────────
SQUARES_X     = 5        # columns of squares
SQUARES_Y     = 7        # rows of squares
MARKER_LENGTH = 0.02     # ArUco marker side  (metres)
SQUARE_LENGTH = 0.04     # chessboard square side (metres)
ARUCO_DICT_ID = cv2.aruco.DICT_4X4_50


# Name → OpenCV constant lookup, so the CLI can accept a human-readable
# dictionary name. Only the dictionaries plausibly used for a calibration
# board are exposed.
ARUCO_DICT_NAMES = {
    "DICT_4X4_50":   cv2.aruco.DICT_4X4_50,
    "DICT_4X4_100":  cv2.aruco.DICT_4X4_100,
    "DICT_4X4_250":  cv2.aruco.DICT_4X4_250,
    "DICT_4X4_1000": cv2.aruco.DICT_4X4_1000,
    "DICT_5X5_50":   cv2.aruco.DICT_5X5_50,
    "DICT_5X5_100":  cv2.aruco.DICT_5X5_100,
    "DICT_5X5_250":  cv2.aruco.DICT_5X5_250,
    "DICT_5X5_1000": cv2.aruco.DICT_5X5_1000,
    "DICT_6X6_50":   cv2.aruco.DICT_6X6_50,
    "DICT_6X6_100":  cv2.aruco.DICT_6X6_100,
    "DICT_6X6_250":  cv2.aruco.DICT_6X6_250,
    "DICT_6X6_1000": cv2.aruco.DICT_6X6_1000,
}


def create_board(squares_x: int = SQUARES_X,
                 squares_y: int = SQUARES_Y,
                 square_length: float = SQUARE_LENGTH,
                 marker_length: float = MARKER_LENGTH,
                 aruco_dict_id: int = ARUCO_DICT_ID,
                 legacy_pattern: bool = False):
    """
    Create and return (ChArUco board, ArUco dictionary).

    For non-square physical cells (e.g. a printed A3 board with
    31 mm wide × 33 mm tall cells), pass `square_length` equal to the
    **X** dimension here and then supply `y_scale = length_y / length_x`
    to `calibrate_charuco()` so the solver uses the correct 3-D object
    points in Y. OpenCV's CharucoBoard only supports a single isotropic
    square length at construction time, so this two-step approach is
    how we handle rectangular cells without monkey-patching the board.

    `legacy_pattern=True` switches the board to OpenCV's pre-4.6
    marker-ID-to-position convention. Many third-party PDF generators
    (calib.io, chev.me, older Python scripts) still use that layout.
    Symptom of a mismatch: detectMarkers finds every marker cleanly
    but CharucoDetector.detectBoard returns zero interior corners,
    because OpenCV's post-4.6 default expects a different id ordering.
    """
    aruco_dict = cv2.aruco.getPredefinedDictionary(aruco_dict_id)
    board = cv2.aruco.CharucoBoard(
        (squares_x, squares_y),
        square_length,
        marker_length,
        aruco_dict,
    )
    if legacy_pattern:
        board.setLegacyPattern(True)
    return board, aruco_dict


def render_board_image(board, pixel_width=800, margin=30):
    """
    Render the board to a BGR image of the given pixel width.

    margin : white-space margin around the board (pixels).

    The aspect ratio is read from the board object itself (rather than
    the module-level SQUARES_X/Y constants) so that rendering is
    correct even when a custom board with different dimensions has
    been created via `create_board(squares_x=…, squares_y=…)`.
    """
    size = board.getChessboardSize()   # (cols, rows)
    cols, rows = int(size[0]), int(size[1])
    aspect = rows / cols
    pixel_height = int(pixel_width * aspect)
    img = board.generateImage(
        (pixel_width, pixel_height), marginSize=margin, borderBits=1
    )
    if len(img.shape) == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def render_board_png_bytes(board, pixel_width=1200, margin=30) -> bytes:
    """
    Render the board and return it as PNG-encoded bytes
    (suitable for serving over HTTP).
    """
    img = render_board_image(board, pixel_width, margin)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode board image as PNG")
    return buf.tobytes()


class PatternWindow:
    """
    Displays the ChArUco board in a borderless, always-on-top OpenCV
    window.  Only used for *local* display (not iPad mode).
    """

    WINDOW_NAME = "MDCT Calibration Target"

    def __init__(self, board, display_width=900):
        self.board = board
        self.display_width = display_width
        self.image = render_board_image(board, display_width)
        self._running = True

    def show(self):
        """Create and display the borderless window. Call from main thread."""
        cv2.namedWindow(self.WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(self.WINDOW_NAME, cv2.WND_PROP_TOPMOST, 1)
        cv2.imshow(self.WINDOW_NAME, self.image)

    def update(self):
        """Call periodically from main loop to keep the window alive."""
        if self._running:
            cv2.imshow(self.WINDOW_NAME, self.image)

    def destroy(self):
        self._running = False
        cv2.destroyWindow(self.WINDOW_NAME)


# ── Convenience: scaled object points for a known physical board size ───────

def get_object_points(board, board_width_mm):
    """
    Return the 3D object points for the ChArUco corners, scaled so
    coordinates are in millimetres matching the physical board width
    displayed on the user's screen.
    """
    pts = board.getChessboardCorners()               # (N, 3)  float32
    canonical_width = SQUARES_X * SQUARE_LENGTH       # metres
    scale = (board_width_mm / 1000.0) / canonical_width
    return (pts * scale).astype(np.float32)
