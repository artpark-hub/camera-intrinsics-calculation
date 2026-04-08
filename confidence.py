"""
Calibration Confidence Scoring
==============================
Combines the calibration result, the per-frame errors, the health-check
block, and the QualityTracker bin distribution into a single 0-100
confidence score with a letter grade and a heuristic uncertainty
estimate for the focal length.

This is **not** a Cramér-Rao bound and **not** the output of a bootstrap
resampling — it is an explicit, transparent heuristic that weights the
factors which empirically correlate with intrinsic-matrix accuracy:

  * edge-gap     (corner coverage governs distortion certainty)
  * diversity    (how varied the captures were)
  * axis fill    (whether every quality axis was actually exercised)
  * RMS          (solver fit health — too low = overfit, too high = bad)
  * sanity       (distortion coefficients in physically plausible ranges)
  * sample count (more is better, with diminishing returns past 30)

The fx uncertainty estimate is purely internal — it is derived from
the coverage, RMS, and distortion metrics above and has NO external
ground-truth anchor. Treat it as a first-order indicator that tracks
how well-constrained the solver was, not as a hard error bar.
"""

from __future__ import annotations

from typing import Any

import numpy as np


# ── Component sub-scores (each in 0..1) ──────────────────────────────────────

def _edge_gap_subscore(edge_gap_frac: float) -> float:
    """
    Realistic curve for handheld iPad / phone calibration where the
    closest detectable corner is structurally limited by the screen
    bezel, the marker quiet-zone, and the detector dropping clipped
    markers near the image border. With this curve:

      0%  → 1.00
      5%  → 0.80
      10% → 0.60
      15% → 0.40
      20% → 0.20
      25% → 0.00

    The previous (15%-floor) curve was tuned for printed boards held
    flat on a desk and produced a 0.00 score for any handheld run.
    """
    return max(0.0, 1.0 - (edge_gap_frac / 0.25))


def _rms_subscore(rms: float) -> float:
    """
    Bell-shaped around the healthy 0.20-0.50 px range.

    < 0.10  → 0.40 (suspiciously low; likely overfit on a small region)
    < 0.20  → 0.85
    0.20-0.50 → 1.00 (sweet spot)
    < 0.80  → 0.80
    < 1.20  → 0.50
    < 2.00  → 0.20
    ≥ 2.00  → 0.05
    """
    if rms < 0.10:
        return 0.40
    if rms < 0.20:
        return 0.85
    if rms <= 0.50:
        return 1.00
    if rms <= 0.80:
        return 0.80
    if rms <= 1.20:
        return 0.50
    if rms <= 2.00:
        return 0.20
    return 0.05


def _sample_count_subscore(n: int) -> float:
    """Saturates at 30 samples."""
    return float(min(1.0, max(0, n) / 30.0))


def _axis_fill_subscore(progress: dict[str, float]) -> float:
    """Mean of per-axis fill fractions (each = bins ≥ min_per_bin / n_bins)."""
    if not progress:
        return 0.0
    return float(sum(progress.values()) / len(progress))


def _distortion_sanity_subscore(dist: np.ndarray) -> tuple[float, list[str]]:
    """
    Returns (subscore in 0..1, list of plain-text issues).
    """
    flat = np.asarray(dist).flatten()
    k1 = float(flat[0]) if len(flat) > 0 else 0.0
    k2 = float(flat[1]) if len(flat) > 1 else 0.0
    p1 = float(flat[2]) if len(flat) > 2 else 0.0
    p2 = float(flat[3]) if len(flat) > 3 else 0.0
    k3 = float(flat[4]) if len(flat) > 4 else 0.0

    score = 1.0
    issues: list[str] = []

    if abs(k1) > 1.5:
        score -= 0.5
        issues.append(f"|k1|={abs(k1):.2f} > 1.5 (extreme)")
    elif abs(k1) > 1.0:
        score -= 0.2
        issues.append(f"|k1|={abs(k1):.2f} > 1.0 (high)")

    if abs(k2) > 5.0:
        score -= 0.4
        issues.append(f"|k2|={abs(k2):.2f} > 5.0 (extreme)")
    elif abs(k2) > 1.0:
        score -= 0.15
        issues.append(f"|k2|={abs(k2):.2f} > 1.0 (high)")

    if abs(p1) > 0.01 or abs(p2) > 0.01:
        score -= 0.20
        issues.append(f"tangential |p1|={abs(p1):.4f} |p2|={abs(p2):.4f} > 0.01")

    if abs(k3) > 1.0:
        score -= 0.30
        issues.append(f"|k3|={abs(k3):.2f} > 1.0 (k3 should normally be fixed at 0)")

    return max(0.0, score), issues


# ── Heuristic fx uncertainty estimate ────────────────────────────────────────

def _heuristic_fx_uncertainty_pct(
    diversity_score: float,
    edge_gap_frac: float,
    rms: float,
    axis_fill: float,
) -> float:
    """
    Returns an estimate of the fractional error in fx, in percent.

    Purely internal heuristic — NO external ground-truth anchor.
    It penalises runs that are poorly constrained by the data:
    low diversity, large edge gap, high RMS, or clustered axes.
    Capped at 15%. Edge-gap is weighted most heavily because corner
    coverage empirically dominates the solver's ability to pin down
    focal length.
    """
    base = 0.5  # well-constrained floor
    base += (1.0 - max(0.0, min(1.0, diversity_score))) * 5.0
    # Edge-gap slope returned to 3.0 after empirically discovering the
    # 5.0 value made handheld iPad runs structurally unable to score
    # below ~8% fx uncertainty (the gap floor is physical, not solver).
    base += min(max(0.0, edge_gap_frac), 0.20) / 0.20 * 3.0
    base += max(0.0, rms - 0.30) * 2.0
    base += (1.0 - max(0.0, min(1.0, axis_fill))) * 3.0
    return float(min(15.0, max(0.5, base)))


# ── Main entry point ─────────────────────────────────────────────────────────

_WEIGHTS = {
    # Rebalanced to stop the structurally-floored edge_gap metric from
    # dominating the grade on handheld iPad calibrations. Diversity and
    # axis_fill (both reachable with effort) take up the slack.
    "edge_gap": 0.20,
    "diversity": 0.20,
    "axis_fill": 0.20,
    "rms": 0.15,
    "distortion_sanity": 0.15,
    "sample_count": 0.10,
}


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 45:
        return "D"
    return "F"


def _verdict(score: float) -> str:
    if score >= 90:
        return "Excellent — production-ready"
    if score >= 75:
        return "Good — suitable for most use"
    if score >= 60:
        return "Marginal — usable, but recapture if accuracy is critical"
    if score >= 45:
        return "Poor — recapture before relying on these intrinsics"
    return "Failing — do not use; the data does not constrain the solver"


def compute_confidence(
    rms: float,
    K: np.ndarray,
    dist: np.ndarray,
    health: dict[str, Any],
    tracker_dict: dict[str, Any],
    per_frame_errors: list[float] | None = None,
) -> dict[str, Any]:
    """
    Build a confidence assessment for the completed calibration.

    Parameters
    ----------
    rms              : final RMS reprojection error (px)
    K                : 3x3 intrinsic matrix
    dist             : 1x5 distortion coefficients
    health           : the health dict produced by calibrator._calibration_health()
    tracker_dict     : QualityTracker.as_dict() snapshot
    per_frame_errors : optional list of per-frame RMS values
    """
    # ── Pull inputs ─────────────────────────────────────────────────────────
    edge_gap_frac = float(health.get("edge_gap", 0.0))
    diversity_score = float(tracker_dict.get("diversity_score", 0.0))
    n_samples = int(tracker_dict.get("total_samples", 0))
    progress = {
        ax: float(info.get("fill_fraction", 0.0))
        for ax, info in tracker_dict.get("axes", {}).items()
    }
    coverage = {
        ax: float(info.get("coverage", 0.0))
        for ax, info in tracker_dict.get("axes", {}).items()
    }
    axis_fill = _axis_fill_subscore(progress)

    # ── Sub-scores ──────────────────────────────────────────────────────────
    sub = {
        "edge_gap": _edge_gap_subscore(edge_gap_frac),
        "diversity": diversity_score,
        "axis_fill": axis_fill,
        "rms": _rms_subscore(rms),
        "sample_count": _sample_count_subscore(n_samples),
    }
    sanity_score, sanity_issues = _distortion_sanity_subscore(dist)
    sub["distortion_sanity"] = sanity_score

    # ── Weighted score 0..100 ───────────────────────────────────────────────
    weighted = sum(_WEIGHTS[k] * sub[k] for k in _WEIGHTS)
    score_100 = float(weighted * 100.0)

    # ── Heuristic fx uncertainty ────────────────────────────────────────────
    fx_unc_pct = _heuristic_fx_uncertainty_pct(
        diversity_score=diversity_score,
        edge_gap_frac=edge_gap_frac,
        rms=rms,
        axis_fill=axis_fill,
    )

    # ── Identify worst factors (for human-readable summary) ─────────────────
    factor_contributions = {
        k: round(_WEIGHTS[k] * sub[k] * 100.0, 2) for k in _WEIGHTS
    }
    factor_max = {k: round(_WEIGHTS[k] * 100.0, 2) for k in _WEIGHTS}
    factor_loss = {
        k: round(factor_max[k] - factor_contributions[k], 2) for k in _WEIGHTS
    }
    worst_factors = sorted(
        factor_loss.items(), key=lambda kv: kv[1], reverse=True
    )[:3]

    # ── Per-frame error stats (optional) ────────────────────────────────────
    pf_stats = None
    if per_frame_errors:
        arr = np.asarray(per_frame_errors, dtype=float)
        pf_stats = {
            "mean": float(arr.mean()),
            "std": float(arr.std()),
            "max": float(arr.max()),
            "min": float(arr.min()),
            "n": int(arr.size),
        }

    return {
        "score": round(score_100, 1),
        "grade": _grade(score_100),
        "verdict": _verdict(score_100),
        "fx_uncertainty_pct_estimate": round(fx_unc_pct, 2),
        "fx_uncertainty_note": (
            "Heuristic estimate of fractional error in fx (and fy, since "
            "aspect ratio is fixed). NOT a Cramér-Rao bound or bootstrap "
            "result, and NOT anchored to any external ground truth. Derived "
            "purely from internal coverage, RMS, and distortion metrics — "
            "treat as a first-order indicator of how well-constrained the "
            "solver was, not as a hard error bar."
        ),
        "weights": _WEIGHTS,
        "subscores": {k: round(v, 3) for k, v in sub.items()},
        "factor_contributions": factor_contributions,
        "factor_max": factor_max,
        "factor_loss": factor_loss,
        "worst_factors": [
            {"factor": k, "points_lost": v} for k, v in worst_factors if v > 0.5
        ],
        "distortion_sanity_issues": sanity_issues,
        "axis_coverage": coverage,
        "axis_fill_fraction": progress,
        "per_frame_error_stats": pf_stats,
    }


# ── Pretty printer ──────────────────────────────────────────────────────────

def print_confidence_summary(conf: dict[str, Any]) -> None:
    """Print the confidence assessment to stdout."""
    print("\n" + "=" * 60)
    print("  CALIBRATION CONFIDENCE")
    print("=" * 60)
    print(f"\n  Overall score : {conf['score']:>5.1f} / 100   ({conf['grade']})")
    print(f"  Verdict       : {conf['verdict']}")
    print(f"  Estimated fx error (heuristic): ±{conf['fx_uncertainty_pct_estimate']:.1f}%")
    print()
    print("  Component breakdown:")
    print(f"    {'factor':<20s} {'sub':>6s} {'×wt':>6s} {'pts':>7s} {'/max':>7s}")
    for k, w in conf["weights"].items():
        sub = conf["subscores"][k]
        pts = conf["factor_contributions"][k]
        mx = conf["factor_max"][k]
        bar_len = int(round((pts / mx) * 20)) if mx > 0 else 0
        bar = "█" * bar_len + "·" * (20 - bar_len)
        print(f"    {k:<20s} {sub:>6.2f} {w:>6.2f} {pts:>6.1f}  {mx:>6.1f}  {bar}")

    if conf["worst_factors"]:
        print("\n  Largest point losses:")
        for wf in conf["worst_factors"]:
            print(f"    - {wf['factor']:<20s}  -{wf['points_lost']:.1f} pts")

    if conf["distortion_sanity_issues"]:
        print("\n  Distortion sanity issues:")
        for issue in conf["distortion_sanity_issues"]:
            print(f"    ! {issue}")

    print("\n  Per-axis bin fill (≥ min_per_bin):")
    for ax, frac in conf["axis_fill_fraction"].items():
        cov = conf["axis_coverage"][ax]
        bar = "█" * int(round(frac * 20)) + "·" * (20 - int(round(frac * 20)))
        print(f"    {ax:<6s} fill={frac:.2f}  touched={cov:.2f}  {bar}")

    print("=" * 60)
