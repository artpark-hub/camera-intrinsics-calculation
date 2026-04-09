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
import os
import platform
import shutil
import subprocess
import threading
import time

import cv2
import numpy as np


# ── Capture-acknowledgment sound ────────────────────────────────────────────
#
# A very short, non-speech "ping" played the instant a sample is captured.
# This is critical UX: the user is standing in front of the camera holding
# a board and cannot read the console. Without an audible ack they have no
# idea whether the script is even seeing them — every voice prompt sounds
# the same regardless of whether the previous attempt succeeded.
#
# Implementation notes:
#   - On macOS we use ``afplay`` with a built-in system sound. afplay
#     uses the system audio mixer, NOT the TTS channel, so it does not
#     block or get blocked by ``say`` — both can play simultaneously.
#   - On Linux we try ``paplay`` then ``aplay`` with whatever .wav we
#     can find under /usr/share/sounds. If nothing is available we fall
#     back to printing the BEL character (terminal beep).
#   - If nothing works, the function silently no-ops.

def _detect_capture_ping():
    """
    Return a callable ``ping()`` that plays a short capture-ack sound
    asynchronously, or a no-op if no sound system is available.
    """
    system = platform.system()

    if system == "Darwin":
        afplay = shutil.which("afplay")
        # Tink is short (~150ms), distinctive, and present on every macOS.
        # Pop and Glass are alternatives if a different feel is wanted.
        sound = "/System/Library/Sounds/Tink.aiff"
        if afplay and os.path.exists(sound):
            def ping():
                # Detached, fully async — fire-and-forget.
                subprocess.Popen(
                    [afplay, sound],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return ping

    if system == "Linux":
        for player in ("paplay", "aplay"):
            cmd = shutil.which(player)
            if not cmd:
                continue
            for candidate in (
                "/usr/share/sounds/freedesktop/stereo/message.oga",
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
                "/usr/share/sounds/alsa/Front_Center.wav",
            ):
                if os.path.exists(candidate):
                    def ping(_cmd=cmd, _snd=candidate):
                        subprocess.Popen(
                            [_cmd, _snd],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    return ping

    # Last-resort cross-platform fallback: terminal bell. Many terminals
    # mute it by default, but it costs nothing to try.
    def ping():
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass
    return ping


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

    def __init__(self, min_captures=20, voice_cooldown=4.0,
                 user_facing_camera=True):
        self.min_captures = min_captures
        self.voice_cooldown = voice_cooldown
        # If True, the user is standing *facing* the camera (standard setup).
        # The camera image is then mirrored left-right relative to the user's
        # body, so when we want the board to land on the right side of the
        # image we must tell the user to move to their LEFT (and vice-versa).
        # Set this False for selfie-style / moving-with-camera setups where
        # image X and body X align directly.
        self.user_facing_camera = user_facing_camera

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

        # Short non-speech "ping" played on every captured sample. Plays
        # in parallel with TTS via a separate audio channel (afplay on
        # macOS) so it never blocks or gets blocked by speech.
        self._capture_ping = _detect_capture_ping()

        # Per-bin progress tracking for Tier-2 quantified feedback. Maps
        # (axis, bin_idx) -> the count this bin had the last time we
        # spoke a suggestion for it. Used to detect whether the previous
        # prompt actually moved the needle so we can speak "good, one
        # more" vs "still need that, try a bigger move".
        self._last_bin_count: dict[tuple[str, int], int] = {}

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

    def play_capture_ping(self):
        """
        Play the short non-speech capture-acknowledgment sound. Always
        fires — bypasses the speech cooldown entirely because it uses a
        separate audio channel (afplay/paplay), not the TTS subprocess.
        Safe to call on every captured sample.
        """
        try:
            self._capture_ping()
        except Exception:
            # Audio subsystem failures must never break the capture loop.
            pass

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

    def on_lock(self):
        """
        Called in auto mode the instant a useful pose is detected, BEFORE
        the capture actually happens. The main loop introduces a short
        hold delay after this so the user has time to freeze.

        Bypasses the normal voice cooldown because it is time-critical —
        if we waited on cooldown, the capture would happen before the
        user heard the instruction, defeating the whole point.
        """
        msg = "Hold still"
        self.current_instruction = msg
        # Force-speak: we cannot afford to skip this even if the cooldown
        # is still active. We still honour the "don't overlap an utterance
        # that is already in flight" check inside _say().
        self._say(msg, rate=200)
        self._last_voice_time = time.time()

    # Once the board has been gone this long, the most likely cause is
    # that the user has walked too far from the camera for the detector
    # to resolve the markers. Switch the spoken prompt accordingly.
    NO_DETECT_TOO_FAR = 5.0   # seconds

    def on_no_detection(self):
        """
        Called when no board is detected in the current frame.

        Three regimes:
          - elapsed < NO_DETECT_GRACE   → keep current instruction
            (brief dropout while transitioning between poses)
          - elapsed < NO_DETECT_TOO_FAR → generic "move it into view"
          - elapsed ≥ NO_DETECT_TOO_FAR → "step closer, the camera
            cannot see the markers from that distance" (force-spoken,
            because this is a recoverable failure mode the user will
            otherwise just sit in)
        """
        elapsed = time.time() - self._last_detection_time
        if elapsed < self.NO_DETECT_GRACE and self.captured_count > 0:
            # Board was seen recently — the user is probably in transition.
            # Keep the current instruction (likely a useful suggestion).
            return

        if elapsed >= self.NO_DETECT_TOO_FAR and self.captured_count > 0:
            msg = (
                "I cannot see the markers from there. "
                "Please step closer to the camera, around one metre or less, "
                "so the pattern is large enough to detect."
            )
            self.current_instruction = msg
            # Force-speak using the cooldown so we don't spam every frame,
            # but bypass the suggestion-priority logic — this is critical.
            if self._can_speak():
                self._say(msg, rate=170)
                self._last_voice_time = time.time()
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

    # ── Targeted per-bin guidance ───────────────────────────────────────────
    #
    # These instructions are generated from QualityTracker.get_starved_bin()
    # — they name the exact physical move the user has to make to fill the
    # most-starved bin, rather than giving generic axis-level hints. This
    # dramatically tightens capture technique once the easy poses are done.
    #
    # `n_bins` is passed in because the semantic meaning of "bin 0" / "bin n-1"
    # depends on how many bins there are (bin 0 is always the extreme low
    # end, bin n-1 the extreme high end).

    def _bin_instruction(self, axis: str, bin_idx: int, n_bins: int) -> str:
        """
        Return a precise imperative for the given (axis, bin) shortfall.

        All directional wording is in the **user's egocentric frame** —
        "your left", "your right" refer to the user's physical body, not
        to the camera image. The X axis is internally mirrored when
        `user_facing_camera=True` so that the user's body movement
        actually puts the board in the right image bin.
        """
        last = n_bins - 1
        mid = n_bins // 2
        is_low = bin_idx == 0
        is_high = bin_idx == last
        is_near_low = bin_idx < mid
        is_near_high = bin_idx > mid

        if axis == "x":
            # Image-X bin 0 = LEFT side of camera image. When user faces
            # the camera, the image is mirrored, so the user's RIGHT hand
            # appears on the LEFT of the image. Translate accordingly.
            if self.user_facing_camera:
                # Flip: low image-X = user's right, high image-X = user's left
                user_low = "right"
                user_high = "left"
            else:
                user_low = "left"
                user_high = "right"

            if is_low:
                return (f"Move the iPad far to your {user_low}, "
                        f"out toward your {user_low} hand side")
            if is_high:
                return (f"Move the iPad far to your {user_high}, "
                        f"out toward your {user_high} hand side")
            if is_near_low:
                return f"Step a little to your {user_low}"
            if is_near_high:
                return f"Step a little to your {user_high}"
            return "Hold the iPad straight in front of you"

        if axis == "y":
            # Vertical is NOT mirrored — works the same in either frame.
            if is_low:
                return "Hold the iPad high above your head"
            if is_high:
                return "Hold the iPad low, down near your knees"
            if is_near_low:
                return "Raise the iPad a bit, above your shoulders"
            if is_near_high:
                return "Lower the iPad a bit, down toward your waist"
            return "Hold the iPad at chest height"

        if axis == "size":
            if is_low:
                return "Step well back from the camera, so the board looks small"
            if is_high:
                return "Walk close to the camera, until the board fills the view"
            if is_near_low:
                return "Take a step back from the camera"
            if is_near_high:
                return "Take a step closer to the camera"
            return "Hold the iPad at arm's length from the camera"

        if axis == "skew":
            if is_low:
                return "Hold the iPad flat, facing the camera directly"
            if is_high:
                return "Tilt the iPad steeply, angle one side far away from the camera"
            if is_near_low:
                return "Give the iPad a gentle tilt"
            if is_near_high:
                return "Tilt the iPad more sharply"
            return "Give the iPad a moderate tilt"

        return "Move the iPad to a new position and orientation"

    def suggest_bin(self, axis: str, bin_idx: int, n_bins: int,
                    current: int | None = None,
                    needed: int | None = None,
                    force: bool = False) -> str | None:
        """
        Speak a targeted instruction for a specific starved bin.

        axis     : "x", "y", "size" or "skew"
        bin_idx  : which bin is starved (0 = low extreme, n_bins-1 = high)
        n_bins   : total bins per axis (so we can translate idx into semantics)
        current  : current sample count in this bin (for progress feedback).
                   When None, we fall back to the original behaviour of just
                   speaking the base instruction with no progress narrative.
        needed   : how many more samples this bin still needs to fill
                   (= min_per_bin - current). Same fall-back as above.
        force    : if True, bypass the voice cooldown (use sparingly — for
                   major transitions such as "now targeting axis_fill")

        Returns the spoken text, or None if cooldown blocked it.

        ── Tier-2 quantified-progress logic ──────────────────────────────
        When ``current`` and ``needed`` are supplied, the message becomes
        progress-aware. We compare ``current`` against the count this bin
        had the *last time* we spoke a suggestion for it, and pick one of
        three modes:

          1. **First ask** — never spoken about this bin before. Speak the
             full instruction plus the count: "Tilt up. Need 2 more."
          2. **Made progress** — count increased since the last prompt.
             Acknowledge the progress and ask for the remainder:
             "Good. One more like that please."
          3. **No progress** — count unchanged since the last prompt.
             Escalate the instruction to ask for a larger move:
             "Still need that. Try a bigger move."

        This addresses the user's complaint that the original prompts
        repeated identically with zero acknowledgment of action.
        """
        if not (force or self._can_speak()):
            return None

        base = self._bin_instruction(axis, bin_idx, n_bins)

        # Legacy behaviour: no count info supplied → speak the bare
        # instruction. Keeps old call sites working unchanged.
        if current is None or needed is None:
            msg = base
        else:
            key = (axis, bin_idx)
            prev = self._last_bin_count.get(key)

            if prev is None:
                # First ask for this bin.
                if needed <= 1:
                    msg = f"{base}. Need one more like this."
                else:
                    msg = f"{base}. Need {needed} more like this."
            elif current > prev:
                # Sample(s) landed in this bin since last ask. Don't
                # repeat the (potentially long) base instruction — keep
                # it short and rewarding.
                if needed <= 1:
                    msg = "Good. One more like that please."
                else:
                    msg = f"Good. {needed} more like that please."
            else:
                # No progress since last ask — escalate the wording so
                # the user knows the previous attempt didn't register.
                msg = f"Still need this one. {base}, with a bigger move."

            self._last_bin_count[key] = current

        self.current_instruction = msg
        self._say(msg)
        self._last_voice_time = time.time()
        return msg

    # ── Reward voice: positive feedback for filling hard bins ──────────────

    def celebrate_bin(self, axis: str, bin_idx: int, n_bins: int) -> None:
        """
        Speak a short reward when a previously-requested (or given-up-on)
        bin finally gets a sample. This gives the user positive feedback
        that their movement worked — critical when they are standing in
        front of the camera with no visual feedback available.

        Bypasses the normal voice cooldown because it is a reaction to
        a specific user action and should feel immediate.
        """
        last = n_bins - 1
        if axis == "x":
            # Translate image-X bin into user-frame wording using the
            # same mirror rule as _bin_instruction.
            if self.user_facing_camera:
                side_low, side_high = "right", "left"
            else:
                side_low, side_high = "left", "right"
            if bin_idx == 0:
                label = f"far {side_low}"
            elif bin_idx == last:
                label = f"far {side_high}"
            elif bin_idx < n_bins // 2:
                label = side_low
            else:
                label = side_high
            msg = f"Got it! {label.capitalize()} position captured."
        elif axis == "y":
            if bin_idx == 0:
                label = "high overhead"
            elif bin_idx == last:
                label = "low near the knees"
            elif bin_idx < n_bins // 2:
                label = "raised"
            else:
                label = "lowered"
            msg = f"Got it! {label.capitalize()} position captured."
        elif axis == "size":
            if bin_idx == 0:
                label = "far distance"
            elif bin_idx == last:
                label = "close up"
            elif bin_idx < n_bins // 2:
                label = "stepped back"
            else:
                label = "stepped closer"
            msg = f"Got it! {label.capitalize()} captured."
        elif axis == "skew":
            if bin_idx == 0:
                label = "flat pose"
            elif bin_idx == last:
                label = "steep tilt"
            else:
                label = "tilted pose"
            msg = f"Got it! {label.capitalize()} captured."
        else:
            msg = "Got it!"

        self.current_instruction = msg
        self._say(msg, rate=185)
        self._last_voice_time = time.time()
        # Bin filled — drop its progress-tracking entry so a future
        # request for a different bin on the same axis starts fresh.
        self._last_bin_count.pop((axis, bin_idx), None)

    # ── Give-up voice with axis-specific actionable advice ────────────────

    def on_give_up(self, axis: str, bin_idx: int, n_bins: int) -> str:
        """
        Speak the "skipping this bin" message, plus an axis-specific
        recovery hint the user can act on. Returns the spoken text for
        logging.
        """
        last = n_bins - 1

        # Axis-specific recovery hint — the key insight is that edges
        # (X, Y extremes) become easier the CLOSER the user stands to
        # the camera, because arm-reach covers a larger image angle.
        if axis in ("x", "y") and bin_idx in (0, last):
            hint = ("Try stepping much closer to the camera, "
                    "around 50 centimetres, and sweeping from there. "
                    "Close range makes the edges easy to reach.")
        elif axis == "size" and bin_idx == 0:
            hint = ("Cannot get far enough back for this shot. "
                    "Moving on — it is not critical.")
        elif axis == "size" and bin_idx == last:
            hint = ("Cannot get close enough for this shot. "
                    "Check that the camera can still focus at close range.")
        elif axis == "skew":
            hint = ("Tilt angle unreachable. "
                    "Moving on — try a steeper tilt next time.")
        else:
            hint = "Moving on to the next target."

        msg = f"Skipping that target. {hint}"
        self.current_instruction = msg
        self._say(msg, rate=165)
        self._last_voice_time = time.time()
        return msg

    def on_struggling(self) -> None:
        """
        Emergency recovery prompt fired when the system has given up on
        several bins in a row — usually a sign that the user is
        standing too far from the camera or the camera's FOV is
        mismatched to the room geometry. Tells the user to reposition.
        """
        msg = ("I am struggling to guide you from here. "
               "Please step much closer to the camera, "
               "about 50 centimetres away, "
               "and sweep the iPad across the full view from that distance. "
               "The image edges become easy to reach from close range.")
        self.current_instruction = msg
        self._say(msg, rate=160)
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
