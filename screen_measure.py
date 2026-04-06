"""
Screen / Device Measurement Utility
====================================
Computes the physical width (mm) of the displayed ChArUco board.

*  On macOS it can auto-detect via Quartz / CoreGraphics.
*  For iPad / tablet display mode it uses a lookup of known device
   screen dimensions.
*  Falls back to a user-supplied ``--board_width_mm`` value.
"""

import numpy as np


# ── Known iPad / tablet screen dimensions ────────────────────────────────────
# Keys are user-friendly short names passed via --ipad_model.
# All measurements are for the *active display area* (no bezels).
#
#   screen_mm : (width, height) in mm when held in PORTRAIT orientation
#   css_vp    : (width, height) CSS logical px in portrait (Safari)
#   dpr       : devicePixelRatio (retina multiplier)
#   ppi       : pixels-per-inch of the physical panel

KNOWN_DEVICES = {
    "ipad-pro-11": {
        "name": "iPad Pro 11″ (1st / 2nd / 3rd / 4th gen)",
        "screen_mm": (178.5, 247.6),
        "css_vp": (834, 1194),
        "dpr": 2,
        "ppi": 264,
    },
    "ipad-pro-12.9": {
        "name": "iPad Pro 12.9″ (3rd / 4th / 5th / 6th gen)",
        "screen_mm": (214.9, 280.6),
        "css_vp": (1024, 1366),
        "dpr": 2,
        "ppi": 264,
    },
    "ipad-pro-13": {
        "name": "iPad Pro 13″ M4 (2024)",
        "screen_mm": (215.5, 281.6),
        "css_vp": (1032, 1376),
        "dpr": 2,
        "ppi": 264,
    },
    "ipad-air-10.9": {
        "name": "iPad Air 10.9″ (4th / 5th gen)",
        "screen_mm": (174.1, 248.1),
        "css_vp": (820, 1180),
        "dpr": 2,
        "ppi": 264,
    },
    "ipad-air-11": {
        "name": "iPad Air 11″ M2 (2024)",
        "screen_mm": (178.5, 247.6),
        "css_vp": (834, 1194),
        "dpr": 2,
        "ppi": 264,
    },
    "ipad-10.2": {
        "name": "iPad 10.2″ (7th–9th gen)",
        "screen_mm": (161.2, 215.0),
        "css_vp": (810, 1080),
        "dpr": 2,
        "ppi": 264,
    },
    "ipad-10.9": {
        "name": "iPad 10.9″ (10th gen, 2022)",
        "screen_mm": (174.1, 248.1),
        "css_vp": (820, 1180),
        "dpr": 2,
        "ppi": 264,
    },
}

# ── Helper: list known devices ───────────────────────────────────────────────

def list_known_devices():
    """Print all supported --ipad_model values."""
    print("\nKnown iPad / tablet models (use with --ipad_model):\n")
    for key, info in KNOWN_DEVICES.items():
        w, h = info["screen_mm"]
        print(f"  {key:22s}  {info['name']:44s}  ({w:.1f} x {h:.1f} mm)")
    print()


# ── iPad board-width computation ─────────────────────────────────────────────

def compute_device_board_width(model_key, pattern_image_width,
                               margin_px=30, border_px=24):
    """
    Compute the physical width (mm) of the ChArUco *pattern area*
    as displayed on a known tablet/iPad.

    Parameters
    ----------
    model_key           : key into KNOWN_DEVICES  (e.g. "ipad-pro-11")
    pattern_image_width : pixel width of the rendered board image
    margin_px           : margin used in ``board.generateImage(marginSize=…)``
    border_px           : CSS border width of the overlay on the web page

    Returns
    -------
    board_width_mm : float
    """
    if model_key not in KNOWN_DEVICES:
        raise ValueError(
            f"Unknown device '{model_key}'.  "
            f"Valid: {', '.join(KNOWN_DEVICES)}"
        )

    dev = KNOWN_DEVICES[model_key]
    screen_w_mm, screen_h_mm = dev["screen_mm"]  # portrait
    vp_w_css, vp_h_css = dev["css_vp"]            # portrait

    # The web page reserves border_px on each side for the overlay,
    # then uses  max-width: calc(100vw - 2*border_px) ; object-fit: contain
    avail_w = vp_w_css - 2 * border_px
    avail_h = vp_h_css - 2 * border_px

    # ChArUco board is 5 columns × 7 rows → aspect = 5/7 ≈ 0.714
    from pattern_display import SQUARES_X, SQUARES_Y
    img_aspect = SQUARES_X / SQUARES_Y

    avail_aspect = avail_w / avail_h
    if img_aspect >= avail_aspect:
        # width-constrained
        display_css_w = avail_w
    else:
        # height-constrained
        display_css_w = avail_h * img_aspect

    mm_per_css_px = screen_w_mm / vp_w_css
    display_mm_w = display_css_w * mm_per_css_px

    # The rendered image has `margin_px` of whitespace on each side.
    # Only the inner pattern area matters for calibration.
    pattern_ratio = (pattern_image_width - 2 * margin_px) / pattern_image_width
    board_width_mm = display_mm_w * pattern_ratio

    return board_width_mm


# ── macOS auto-detection (optional) ──────────────────────────────────────────

def get_screen_mm_per_pixel():
    """
    Query macOS for the main display's physical size and logical resolution.

    Returns
    -------
    (mm_per_pixel, screen_width_mm, screen_height_mm, logical_w, logical_h)
    or None if detection fails or the platform is not macOS.
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


def auto_detect_board_width(display_pixel_width, board_image):
    """
    High-level: auto-detect the physical width of the board as shown
    on the *local* Mac screen (not an iPad).

    Returns board_width_mm (float) or None.
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


def print_screen_info():
    """Print detected screen information for debugging."""
    info = get_screen_mm_per_pixel()
    if info is None:
        print("[INFO] macOS screen auto-detection not available on this platform.")
        print("       Use --board_width_mm or --ipad_model to specify board size.")
        return

    mm_per_px, w_mm, h_mm, lw, lh = info
    print(f"[INFO] Screen detected:")
    print(f"       Physical:  {w_mm:.1f} x {h_mm:.1f} mm")
    print(f"       Logical:   {lw} x {lh} px")
    print(f"       Scale:     {mm_per_px:.4f} mm/px")
