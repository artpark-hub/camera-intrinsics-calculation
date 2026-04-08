"""
Screen / Board Measurement Utility
==================================
Helpers for determining the physical width (mm) of the displayed
ChArUco board.

For iPad / tablet display mode the user supplies ``--board_width_mm``
directly (measured with a ruler — corner of outermost square to corner
of outermost square). This module provides a *local Mac screen*
auto-detect path for the single-screen mode where the pattern is
displayed on the laptop itself.

Note: camera intrinsic calibration via Zhang's method is scale-invariant
in the object points, so an inaccurate board width affects only the
absolute scale of returned tvecs — never fx, fy, cx, cy or the
distortion coefficients. Get it right only if you care about real-world
pose distances.
"""


def auto_detect_board_width(display_pixel_width, board_image):
    """
    Auto-detect the physical width of the board as displayed on the
    *local* Mac screen (not an iPad). Returns board_width_mm (float)
    or None on non-macOS or on detection failure.
    """
    info = get_screen_mm_per_pixel()
    if info is None:
        return None

    mm_per_px = info[0]
    img_w = board_image.shape[1]
    margin = 30
    pattern_px = img_w - 2 * margin
    scale = display_pixel_width / img_w
    displayed_pattern_px = pattern_px * scale
    return displayed_pattern_px * mm_per_px


def get_screen_mm_per_pixel():
    """
    Query macOS for the main display's physical size and logical
    resolution.

    Returns (mm_per_pixel, w_mm, h_mm, logical_w, logical_h) or None.
    """
    try:
        import Quartz

        display_id = Quartz.CGMainDisplayID()
        size_mm = Quartz.CGDisplayScreenSize(display_id)
        logical_w = Quartz.CGDisplayPixelsWide(display_id)
        logical_h = Quartz.CGDisplayPixelsHigh(display_id)

        if logical_w == 0 or size_mm.width == 0:
            return None

        mm_per_px = size_mm.width / logical_w
        return mm_per_px, size_mm.width, size_mm.height, logical_w, logical_h

    except ImportError:
        return None
    except Exception:
        return None


def print_screen_info():
    """Print detected screen information for debugging."""
    info = get_screen_mm_per_pixel()
    if info is None:
        print("[INFO] macOS screen auto-detection not available on this platform.")
        print("       Pass --board_width_mm <measured> to specify the board size.")
        return

    mm_per_px, w_mm, h_mm, lw, lh = info
    print(f"[INFO] Screen detected:")
    print(f"       Physical:  {w_mm:.1f} x {h_mm:.1f} mm")
    print(f"       Logical:   {lw} x {lh} px")
    print(f"       Scale:     {mm_per_px:.4f} mm/px")
