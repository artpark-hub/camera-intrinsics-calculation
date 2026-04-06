"""
Voice Guidance System
=====================
State machine that guides the user through calibration poses via
text-to-speech.  Cross-platform: uses macOS ``say``, Linux ``espeak``
/ ``spd-say``, or Windows SAPI — whichever is available.

Divides the camera FOV into a 9-sector grid and tracks 5 required
orientations.
"""

import math
import platform
import shutil
import subprocess
import threading
import time

import cv2
import numpy as np


# ── 9-sector grid ────────────────────────────────────────────────────────────
SECTORS = {
    "top-left":      (0, 0),
    "top-center":    (0, 1),
    "top-right":     (0, 2),
    "center-left":   (1, 0),
    "center":        (1, 1),
    "center-right":  (1, 2),
    "bottom-left":   (2, 0),
    "bottom-center": (2, 1),
    "bottom-right":  (2, 2),
}

# ── 5 required orientations ─────────────────────────────────────────────────
ORIENTATIONS = ["flat", "tilt-up", "tilt-down", "tilt-left", "tilt-right"]

# Threshold for tilt detection (radians, ~14 degrees)
TILT_THRESHOLD = 0.25


# ── Cross-platform TTS helper ───────────────────────────────────────────────

def _detect_tts_backend():
    """
    Return a callable ``speak(text, rate)`` for the current platform,
    or *None* if no TTS engine is available.
    """
    system = platform.system()

    if system == "Darwin" and shutil.which("say"):
        def speak(text, rate=180):
            subprocess.run(["say", "-r", str(rate), text],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return speak

    if system == "Linux":
        if shutil.which("espeak"):
            def speak(text, rate=180):
                # espeak rate is words-per-minute (default 175)
                subprocess.run(["espeak", "-s", str(rate), text],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return speak
        if shutil.which("spd-say"):
            def speak(text, rate=180):
                subprocess.run(["spd-say", text],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return speak
        if shutil.which("festival"):
            def speak(text, rate=180):
                proc = subprocess.Popen(
                    ["festival", "--tts"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.communicate(input=text.encode())
            return speak

    if system == "Windows" and shutil.which("powershell"):
        def speak(text, rate=180):
            # SAPI rate: -10 … +10 (0 = normal ~200 wpm)
            sapi_rate = max(-5, min(5, (rate - 180) // 10))
            safe = text.replace("'", "''").replace('"', '`"')
            ps = (
                f"Add-Type -AssemblyName System.Speech;"
                f"$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"$s.Rate={sapi_rate};"
                f"$s.Speak('{safe}')"
            )
            subprocess.run(["powershell", "-Command", ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return speak

    # No TTS available — the app still works (visual feedback + print)
    return None


class VoiceGuidance:
    """
    Determines what instruction to give the user and delivers it
    via platform-native TTS (if available).
    """

    # How long (seconds) after the last detection before we consider
    # the board truly "lost" and switch to the no-detection message.
    # Prevents flicker when the board is briefly occluded or the user
    # is transitioning between positions.
    NO_DETECT_GRACE = 2.0

    def __init__(self, min_captures=20, voice_cooldown=4.0):
        self.min_captures = min_captures
        self.voice_cooldown = voice_cooldown

        # Captured zones: set of (sector, orientation)
        self.captured_poses: set[tuple[str, str]] = set()
        self.captured_count = 0

        # Per-sector / per-orientation coverage
        self.sector_counts = {s: 0 for s in SECTORS}
        self.orientation_counts = {o: 0 for o in ORIENTATIONS}

        # Voice timing
        self._last_voice_time = 0.0
        self._voice_thread: threading.Thread | None = None
        self._tts = _detect_tts_backend()

        if self._tts is None:
            print("[INFO] No TTS engine found — voice guidance disabled "
                  "(visual feedback still active)")

        # Current instruction (displayed as text on HUD and iPad)
        self.current_instruction = "Place the screen in view of the camera"
        self.current_sector: str | None = None
        self.current_orientation: str | None = None

        # Hysteresis: timestamp of last successful detection
        self._last_detection_time = 0.0

    # ── TTS ──────────────────────────────────────────────────────────────────

    def _say(self, text: str, rate: int = 180):
        """Non-blocking, platform-independent TTS."""
        if self._tts is None:
            return
        if self._voice_thread and self._voice_thread.is_alive():
            return  # Don't overlap
        self._voice_thread = threading.Thread(
            target=self._tts, args=(text, rate), daemon=True
        )
        self._voice_thread.start()

    def _can_speak(self) -> bool:
        return time.time() - self._last_voice_time > self.voice_cooldown

    def wait_for_speech(self, timeout: float = 10.0):
        """Block until the current TTS utterance finishes."""
        if self._voice_thread and self._voice_thread.is_alive():
            self._voice_thread.join(timeout=timeout)

    # ── Sector / orientation helpers ─────────────────────────────────────────

    def get_sector(self, cx, cy, frame_w, frame_h):
        """Determine which of the 9 sectors the board centre is in."""
        col = min(int((cx / frame_w) * 3), 2)
        row = min(int((cy / frame_h) * 3), 2)
        for name, (r, c) in SECTORS.items():
            if r == row and c == col:
                return name
        return "center"

    def get_orientation(self, rvec):
        """
        Derive one of the five orientations from the board's rotation vector.
        """
        if rvec is None:
            return "flat"

        R, _ = cv2.Rodrigues(rvec)
        normal = R[:, 2]  # board Z-axis in camera frame

        tilt_x = math.atan2(normal[1], normal[2])  # up / down
        tilt_y = math.atan2(normal[0], normal[2])  # left / right

        if abs(tilt_x) < TILT_THRESHOLD and abs(tilt_y) < TILT_THRESHOLD:
            return "flat"
        if tilt_x > TILT_THRESHOLD:
            return "tilt-down"
        if tilt_x < -TILT_THRESHOLD:
            return "tilt-up"
        if tilt_y > TILT_THRESHOLD:
            return "tilt-right"
        if tilt_y < -TILT_THRESHOLD:
            return "tilt-left"
        return "flat"

    # ── Orientation labels for natural-sounding speech ──────────────────────
    _ORIENT_SPEECH = {
        "flat":       "facing the camera directly",
        "tilt-up":    "up",
        "tilt-down":  "down",
        "tilt-left":  "to the left",
        "tilt-right": "to the right",
    }

    # ── Imperative phrasing (diversity score < 0.55) ────────────────────
    _STRONG_HINTS = {
        "size": "Change your distance to the camera. "
                "Walk closer, then walk farther back",
        "skew": "Tilt the iPad more steeply. "
                "Angle it so the top or side is farther from the camera",
        "x":    "Step sideways. Move a few steps to your left, "
                "then to your right",
        "y":    "Change the iPad height. "
                "Hold it up high, then down low",
    }
    # ── Softer phrasing (diversity score >= 0.55, almost done) ────────
    _SOFT_HINTS = {
        "size": "A few more at different distances would help",
        "skew": "A couple of tilted poses would improve quality",
        "x":    "Try one or two captures off to the side",
        "y":    "One more at a different height if you can",
    }

    def get_missing_suggestion(self, quality_progress=None,
                               diversity_score=0.0):
        """
        Return a text suggestion for the next needed pose.

        quality_progress : dict with keys x, y, size, skew (each 0.0-1.0)
        diversity_score  : overall diversity score (0.0-1.0)

        All returned strings are pure ASCII for OpenCV putText().
        """
        # ── Quality-tracker-aware suggestions (highest priority) ─────────
        if quality_progress:
            weakest = min(quality_progress, key=quality_progress.get)
            val = quality_progress[weakest]

            # Only suggest if the axis is meaningfully behind
            if val < 0.85:
                if diversity_score >= 0.55:
                    hint = self._SOFT_HINTS.get(weakest)
                else:
                    hint = self._STRONG_HINTS.get(weakest)
                if hint:
                    return hint

        # ── Fallback: sector / orientation coverage ──────────────────────
        min_sector = min(self.sector_counts, key=self.sector_counts.get)
        min_orient = min(self.orientation_counts, key=self.orientation_counts.get)

        sector_label = min_sector.replace("-", " ").replace("center", "centre")
        orient_label = self._ORIENT_SPEECH.get(min_orient, min_orient)

        if self.orientation_counts[min_orient] == 0:
            return f"Try tilting the screen {orient_label}"
        if self.sector_counts[min_sector] < 2:
            return f"Move the screen to the {sector_label} of the camera view"
        return None

    # ── Event callbacks (called by main loop) ────────────────────────────────

    def on_detection(self, cx, cy, frame_w, frame_h, rvec=None):
        """Called every frame when the board is detected."""
        self._last_detection_time = time.time()
        self.current_sector = self.get_sector(cx, cy, frame_w, frame_h)
        self.current_orientation = (
            self.get_orientation(rvec) if rvec is not None else "flat"
        )

    def on_capture(self, sector, orientation, diversity_score=0.0):
        """Called when a pose is successfully captured."""
        self.captured_count += 1
        self.captured_poses.add((sector, orientation))
        self.sector_counts[sector] = self.sector_counts.get(sector, 0) + 1
        self.orientation_counts[orientation] = (
            self.orientation_counts.get(orientation, 0) + 1
        )

        msg = f"Captured pose {self.captured_count}."
        if self.captured_count < self.min_captures:
            remaining = self.min_captures - self.captured_count
            msg += f" {remaining} more to go."
        elif diversity_score >= 0.55:
            msg += " Almost there, just a bit more variety."

        self.current_instruction = msg
        if self._can_speak():
            self._say(msg)
            self._last_voice_time = time.time()

    def on_stable(self):
        """Called when the board is stable and about to be captured."""
        msg = "Hold steady... capturing."
        self.current_instruction = msg
        if self._can_speak():
            self._say(msg)
            self._last_voice_time = time.time()

    def on_no_detection(self):
        """
        Called when no board is detected in the current frame.

        Uses a grace period so that brief detection dropouts (e.g. while
        the user is moving between positions) don't immediately stomp a
        useful suggestion that was just spoken / displayed.
        """
        elapsed = time.time() - self._last_detection_time
        if elapsed < self.NO_DETECT_GRACE and self.captured_count > 0:
            # Board was seen recently — the user is probably in transition.
            # Keep the current instruction (likely a useful suggestion).
            return
        self.current_instruction = "Move the calibration screen into the camera view"

    def suggest_next_pose(self, quality_progress=None, diversity_score=0.0):
        """
        Periodic guidance for the next needed pose.

        quality_progress : dict from QualityTracker.progress()
        diversity_score  : float from QualityTracker.diversity_score()
        """
        if not self._can_speak():
            return
        suggestion = self.get_missing_suggestion(quality_progress, diversity_score)
        if suggestion:
            self.current_instruction = suggestion
            self._say(suggestion)
            self._last_voice_time = time.time()

    def on_calibration_complete(self):
        """Called when enough samples have been collected."""
        msg = "Calibration data collection complete! Computing intrinsics."
        self.current_instruction = msg
        self._say(msg, rate=160)
        self._last_voice_time = time.time()

    def on_done(self, rms):
        """Called when final calibration is computed."""
        quality = "excellent" if rms < 0.5 else "good" if rms < 1.0 else "poor"
        msg = (
            f"Calibration finished. Quality is {quality} "
            f"with R M S error of {rms:.2f} pixels."
        )
        self.current_instruction = msg
        self._say(msg, rate=160)
