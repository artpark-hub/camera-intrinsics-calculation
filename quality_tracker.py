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
import time

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
        # Per-bin "capture index when we first asked the user to fill this".
        # We use this to give up only after the user has had several
        # captures' worth of time to try — rather than after N voice
        # prompts (which can fire every 8 seconds and trigger a cascade
        # before the user has physically moved). None means "never asked".
        self._bin_first_asked_at: dict[str, list[int | None]] = {
            ax: [None] * n_bins for ax in self.AXES
        }
        # Wall-clock time at which each bin was first asked. Needed as a
        # fallback when the requested action makes detection impossible
        # (e.g. "step far back" → board out of detector range → captures
        # stop landing → capture-based give-up never fires). The bin
        # gets skipped after `give_up_seconds` regardless.
        self._bin_first_asked_time: dict[str, list[float | None]] = {
            ax: [None] * n_bins for ax in self.AXES
        }
        # Track which bins we've already given up on, so we can announce
        # the decision exactly once (and exclude from future selection).
        self._skipped_bins: set[tuple[str, int]] = set()

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
        """
        Record a sample.

        Returns a list of (axis, bin_idx) tuples for any bin that:
          (a) was previously empty,
          (b) had been actively requested (i.e. we'd called
              `mark_attempted` on it at least once), and
          (c) was just filled by this sample.

        The main loop uses that return value to trigger a short
        "celebration" voice prompt — so the user gets positive
        feedback when they finally land a difficult pose, instead
        of hearing only nags and give-up messages.
        """
        newly_filled_asked: list[tuple[str, int]] = []
        for ax in self.AXES:
            idx = self._bin(metrics[ax])
            was_empty = self.bins[ax][idx] == 0
            was_asked = self._bin_first_asked_at[ax][idx] is not None
            was_skipped = (ax, idx) in self._skipped_bins

            self.bins[ax][idx] += 1

            # Successfully landed a sample in this bin — cancel any
            # "give up" flag and reset the first-asked timestamp.
            # If the user finally managed it after many tries, welcome
            # them back.
            self._bin_first_asked_at[ax][idx] = None
            self._bin_first_asked_time[ax][idx] = None
            self._skipped_bins.discard((ax, idx))

            if was_empty and (was_asked or was_skipped):
                newly_filled_asked.append((ax, idx))

        # Track diversity after each capture for stagnation detection
        self._diversity_history.append(self.diversity_score())
        return newly_filled_asked

    # ── Progress & diversity ─────────────────────────────────────────────────

    def progress(self):
        """
        Per-axis fill fraction: fraction of *reachable* bins that have
        reached `min_per_bin`. Skipped (physically unreachable) bins are
        excluded from both the numerator and denominator so a single
        unreachable bin doesn't permanently stall the axis at e.g.
        4/5 = 0.80 and trigger an unending "more variety" prompt.
        """
        out = {}
        for ax in self.AXES:
            reachable_total = 0
            reachable_filled = 0
            for idx in range(self.n):
                if (ax, idx) in self._skipped_bins:
                    continue
                reachable_total += 1
                if self.bins[ax][idx] >= self.min_per_bin:
                    reachable_filled += 1
            out[ax] = (
                1.0 if reachable_total == 0
                else float(reachable_filled / reachable_total)
            )
        return out

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
        #
        # Guard: only fire once every axis has touched at least 60% of
        # its bins. Without this guard, a clustered run can plateau at
        # a mediocre score with whole edge/size regions untouched and
        # still early-exit — which is exactly how we ended up shipping
        # calibrations with empty right-edge / top-edge / far-distance
        # bins.
        if len(self._diversity_history) >= 5:
            min_coverage = min(
                (self.bins[ax] > 0).sum() / self.n for ax in self.AXES
            )
            if min_coverage >= 0.60:
                recent = self._diversity_history[-5:]
                improvement = recent[-1] - recent[0]
                if improvement < 0.02:
                    return True

        return False

    def is_complete(self):
        """True when every bin on every axis is filled (strict, legacy)."""
        return all(v >= 1.0 for v in self.progress().values())

    # ── Targeted guidance: which specific bin is most starved? ──────────────

    def get_starved_bin(self):
        """
        Identify the single most critical under-populated bin across
        all axes, using the axis weights as a tiebreaker.

        Bins in `_skipped_bins` (given up on because the user hasn't
        been able to fill them within a reasonable capture budget) are
        excluded from consideration until the user actually lands a
        sample in them — at which point they are reinstated automatically
        by `update()`.

        Returns
        -------
        (axis, bin_index, deficit) where deficit = shortfall below
        min_per_bin (higher = more urgent). Returns None when every
        remaining under-populated bin has been given up on OR every bin
        already has enough samples.
        """
        best = None   # (urgency, axis, bin_idx, deficit)
        for ax in self.AXES:
            counts = self.bins[ax]
            weight = self._WEIGHTS.get(ax, 0.25)
            for idx, cnt in enumerate(counts):
                if cnt >= self.min_per_bin:
                    continue
                if (ax, idx) in self._skipped_bins:
                    continue
                deficit = self.min_per_bin - int(cnt)
                # Urgency = how empty the bin is, scaled by axis weight.
                # Totally empty bins beat half-empty ones on the same axis.
                urgency = weight * (deficit + (1.0 if cnt == 0 else 0.0))
                if best is None or urgency > best[0]:
                    best = (urgency, ax, idx, deficit)

        if best is None:
            return None
        _, ax, idx, deficit = best
        return (ax, idx, deficit)

    def mark_attempted(self, axis: str, bin_idx: int,
                       min_captures_to_try: int = 5,
                       give_up_seconds: float = 25.0) -> bool:
        """
        Record that we've just asked the user for a specific starved bin.

        Hybrid give-up policy — we skip the bin once **either** of these
        conditions holds (whichever comes first):

          1. **Capture-based**: `min_captures_to_try` further captures
             have landed since the first ask, without this bin filling.
             Scales with real user effort, immune to voice-cooldown
             cascades.

          2. **Wall-clock fallback**: `give_up_seconds` of real time
             have elapsed since the first ask, regardless of whether
             any captures landed at all. This is essential for bins
             whose requested action *prevents* detection — e.g. size
             bin 0 ("step far back") puts the iPad out of detector
             range, so condition (1) alone never trips and the user
             gets stuck listening to the same impossible instruction.

        Returns True exactly once, at the moment the bin transitions
        into the give-up set, so the caller can speak a consolation
        message. Returns False on every other call.
        """
        if axis not in self._bin_first_asked_at:
            return False
        if (axis, bin_idx) in self._skipped_bins:
            return False

        # First time we're asking for this bin → remember when.
        if self._bin_first_asked_at[axis][bin_idx] is None:
            self._bin_first_asked_at[axis][bin_idx] = self.total_samples()
            self._bin_first_asked_time[axis][bin_idx] = time.time()
            return False

        # Already asked previously. Check both give-up conditions.
        first_capture = self._bin_first_asked_at[axis][bin_idx]
        captures_since = self.total_samples() - first_capture

        first_time = self._bin_first_asked_time[axis][bin_idx] or time.time()
        seconds_since = time.time() - first_time

        bin_unfilled = self.bins[axis][bin_idx] < self.min_per_bin
        captures_exhausted = captures_since >= min_captures_to_try
        time_exhausted = seconds_since >= give_up_seconds

        if bin_unfilled and (captures_exhausted or time_exhausted):
            self._skipped_bins.add((axis, bin_idx))
            return True

        return False

    def min_axis_coverage(self):
        """
        Return the smallest per-axis coverage fraction.

        Crucially, bins we have already given up on as physically
        unreachable are **excluded from both numerator and denominator**
        — otherwise the completion gate can never fire once any bin
        has been skipped, and the only exit from the capture loop
        becomes the max_captures safety cap or a user Ctrl-C.

        Coverage is computed as:
            touched_reachable / total_reachable
        where "reachable" = not in `_skipped_bins`. If every bin on
        an axis has been given up on, that axis is treated as complete
        (1.0) so it stops blocking the completion gate.
        """
        per_axis = []
        for ax in self.AXES:
            reachable_touched = 0
            reachable_total = 0
            for idx in range(self.n):
                if (ax, idx) in self._skipped_bins:
                    continue
                reachable_total += 1
                if self.bins[ax][idx] > 0:
                    reachable_touched += 1
            if reachable_total == 0:
                # Every bin on this axis given up on — cannot block.
                per_axis.append(1.0)
            else:
                per_axis.append(reachable_touched / reachable_total)
        return float(min(per_axis)) if per_axis else 0.0

    def total_samples(self):
        return int(self.bins["x"].sum())

    # ── Serialization ────────────────────────────────────────────────────────

    def as_dict(self):
        """
        Return a JSON-serializable snapshot of the tracker state, suitable
        for embedding in calibration.json for post-mortem analysis.

        Includes the raw bin counts, per-axis fill fractions, per-axis
        coverage and entropy contributions, the overall diversity score,
        and the diversity history (one value per accepted capture).
        """
        per_axis = {}
        for ax in self.AXES:
            counts = self.bins[ax]
            total = int(counts.sum())
            touched = int((counts > 0).sum())
            full = int((counts >= self.min_per_bin).sum())
            if total > 0:
                probs = counts[counts > 0] / total
                entropy = float(-np.sum(probs * np.log(probs)))
                max_entropy = float(np.log(self.n)) if self.n > 1 else 1.0
                norm_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
            else:
                norm_entropy = 0.0
            per_axis[ax] = {
                "bins": counts.tolist(),
                "samples": total,
                "bins_touched": touched,
                "bins_full": full,
                "coverage": float(touched / self.n) if self.n > 0 else 0.0,
                "fill_fraction": float(full / self.n) if self.n > 0 else 0.0,
                "norm_entropy": float(norm_entropy),
                "weight": float(self._WEIGHTS.get(ax, 0.0)),
            }
        return {
            "n_bins": self.n,
            "min_per_bin": self.min_per_bin,
            "total_samples": self.total_samples(),
            "diversity_score": float(self.diversity_score()),
            "diversity_history": [float(v) for v in self._diversity_history],
            "axes": per_axis,
        }
