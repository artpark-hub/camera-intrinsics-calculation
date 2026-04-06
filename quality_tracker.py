"""
ROS-style Quality Tracker
=========================
Maintains per-axis histograms to ensure diverse calibration samples.
Ported from the CameraIntrinsics reference project, adapted for ChArUco.

Completion uses a *diversity score* (entropy + coverage) rather than
requiring every single bin to be filled -- so calibration can finish
in 20-30 natural captures without the user chasing impossible edge
positions.
"""

import math

import numpy as np


class QualityTracker:
    """
    Maintains per-axis histograms (like ROS camera_calibration).

    Four quality axes:
        X    -- normalised horizontal position of board centre  [0, 1]
        Y    -- normalised vertical position of board centre    [0, 1]
        Size -- fraction of frame area covered by the board     [0, 1]
        Skew -- perspective distortion from 3-D tilt            [0, 1]
    """

    AXES = ["x", "y", "size", "skew"]

    # Weights for the overall diversity score.
    # X and Size matter most; Y and Skew get lower weight because
    # their extreme bins are physically hardest to reach.
    _WEIGHTS = {"x": 0.35, "y": 0.25, "size": 0.15, "skew": 0.25}

    def __init__(self, n_bins=7, min_per_bin=2):
        self.n = n_bins
        self.min_per_bin = min_per_bin
        self.bins = {ax: np.zeros(n_bins, dtype=int) for ax in self.AXES}
        # Stagnation tracking: last N diversity scores after each capture
        self._diversity_history: list[float] = []

    def _bin(self, value):
        return min(int(value * self.n), self.n - 1)

    # ── Metrics ──────────────────────────────────────────────────────────────

    @staticmethod
    def compute_metrics(corners, frame_w, frame_h):
        """
        Compute the four quality metrics from detected corners.

        corners  : (N, 1, 2) or (N, 2) array of 2D corner positions
        frame_w  : frame width in pixels
        frame_h  : frame height in pixels

        Returns dict with x, y, size, skew -- all in [0, 1].
        """
        pts = corners.reshape(-1, 2)

        # ── Board centre (normalised) ────────────────────────────────
        cx = pts[:, 0].mean() / frame_w
        cy = pts[:, 1].mean() / frame_h

        # ── Size: bounding-box area as fraction of frame ─────────────
        px_w = pts[:, 0].max() - pts[:, 0].min()
        px_h = pts[:, 1].max() - pts[:, 1].min()
        size = (px_w / frame_w) * (px_h / frame_h)

        # ── Skew: perspective distortion from 3-D tilt ───────────────
        canonical_aspect = 5.0 / 7.0  # SQUARES_X / SQUARES_Y
        if px_h > 1e-3:
            observed_aspect = px_w / px_h
        else:
            observed_aspect = canonical_aspect

        aspect_deviation = abs(observed_aspect - canonical_aspect) / canonical_aspect
        skew = min(aspect_deviation / 0.5, 1.0)

        # sqrt scaling spreads typical board sizes (2%-40% of frame)
        # much more evenly across bins than the linear *4 multiplier.
        # sqrt(0.02)*2.5=0.35, sqrt(0.10)*2.5=0.79, sqrt(0.25)*2.5=1.25→1.0
        size_scaled = min(math.sqrt(size) * 2.5, 1.0)

        return dict(
            x=float(cx),
            y=float(cy),
            size=float(size_scaled),
            skew=float(min(skew, 1.0)),
        )

    # ── Sample gating ────────────────────────────────────────────────────────

    def is_useful(self, metrics):
        """
        Return True if this sample would improve diversity.

        A sample is useful if:
        - it fills a bin below min_per_bin  (primary), OR
        - it falls in a bin that is below the axis median count
          (secondary -- improves balance even after min_per_bin is met).
        """
        for ax in self.AXES:
            b = self._bin(metrics[ax])
            if self.bins[ax][b] < self.min_per_bin:
                return True

        # Secondary: accept if it lands in a below-median bin
        for ax in self.AXES:
            b = self._bin(metrics[ax])
            median = float(np.median(self.bins[ax]))
            if self.bins[ax][b] < median:
                return True

        return False

    def update(self, metrics):
        """Record a sample."""
        for ax in self.AXES:
            self.bins[ax][self._bin(metrics[ax])] += 1
        # Track diversity after each capture for stagnation detection
        self._diversity_history.append(self.diversity_score())

    # ── Progress & diversity ─────────────────────────────────────────────────

    def progress(self):
        """Per-axis fill fraction: fraction of bins >= min_per_bin."""
        return {
            ax: float((self.bins[ax] >= self.min_per_bin).sum() / self.n)
            for ax in self.AXES
        }

    def diversity_score(self):
        """
        Overall diversity score (0.0 -- 1.0).

        Combines per-axis *coverage* (fraction of bins touched) and
        *entropy* (how evenly spread the samples are).  A weighted
        average across axes gives the final score.

        - 0.0 = all samples in one bin on every axis
        - 1.0 = perfectly uniform coverage everywhere
        """
        scores = {}
        for ax in self.AXES:
            counts = self.bins[ax]
            total = counts.sum()
            if total == 0:
                scores[ax] = 0.0
                continue

            # Coverage: fraction of bins with at least 1 sample
            touched = (counts > 0).sum()
            coverage = touched / self.n

            # Normalised entropy (0 = all in one bin, 1 = uniform)
            probs = counts[counts > 0] / total
            entropy = -np.sum(probs * np.log(probs))
            max_entropy = np.log(self.n)  # entropy of uniform distribution
            norm_entropy = entropy / max_entropy if max_entropy > 0 else 0.0

            scores[ax] = 0.5 * coverage + 0.5 * norm_entropy

        # Weighted average
        total = sum(self._WEIGHTS[ax] * scores.get(ax, 0.0) for ax in self.AXES)

        # Floor: if any axis has < 25% coverage, cap score at 0.85
        # to prevent completion with a totally neglected axis
        for ax in self.AXES:
            touched = (self.bins[ax] > 0).sum()
            if touched / self.n < 0.25:
                total = min(total, 0.85)
                break

        return float(total)

    def is_good_enough(self, min_samples=20, score_threshold=0.65):
        """
        True when there is enough diverse data for a good calibration.

        This is the primary completion gate -- replaces the old
        ``is_complete()`` which required every bin to be filled.

        Also triggers if the user has collected enough samples but
        diversity has *stagnated* (not improved in the last 5 captures)
        -- meaning they've tried their best and further captures won't
        help without dramatically changing the physical setup.
        """
        n = self.total_samples()
        if n < min_samples:
            return False

        score = self.diversity_score()
        if score >= score_threshold:
            return True

        # Stagnation escape: if the last 5 captures barely improved
        # diversity, accept what we have (the user has tried enough).
        if len(self._diversity_history) >= 5:
            recent = self._diversity_history[-5:]
            improvement = recent[-1] - recent[0]
            if improvement < 0.02:
                return True

        return False

    def is_complete(self):
        """True when every bin on every axis is filled (strict, legacy)."""
        return all(v >= 1.0 for v in self.progress().values())

    def total_samples(self):
        return int(self.bins["x"].sum())
