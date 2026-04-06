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


# ── Board parameters (from design spec) ─────────────────────────────────────
SQUARES_X     = 5        # columns of squares
SQUARES_Y     = 7        # rows of squares
MARKER_LENGTH = 0.02     # ArUco marker side  (metres)
SQUARE_LENGTH = 0.04     # chessboard square side (metres)
ARUCO_DICT_ID = cv2.aruco.DICT_4X4_50


def create_board():
    """Create and return the ChArUco board object + ArUco dictionary."""
    aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT_ID)
    board = cv2.aruco.CharucoBoard(
        (SQUARES_X, SQUARES_Y),
        SQUARE_LENGTH,
        MARKER_LENGTH,
        aruco_dict,
    )
    return board, aruco_dict


def render_board_image(board, pixel_width=800, margin=30):
    """
    Render the board to a BGR image of the given pixel width.

    margin : white-space margin around the board (pixels).
    """
    aspect = SQUARES_Y / SQUARES_X
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
