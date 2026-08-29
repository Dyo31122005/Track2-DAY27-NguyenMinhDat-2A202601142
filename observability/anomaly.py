"""Anomaly detection.

`zscore` and `mad` are kept as explicit, simple baselines. `auto` is the
context-aware detector: same-weekday seasonality, robust median/MAD instead
of mean/std, and an EWMA trend baseline, blended so a single flat window
doesn't have to be "the" answer for every metric shape.

When Z-score gets it wrong
---------------------------
- ``std == 0``: every value in the window is identical (e.g. a quiet metric,
  or too short a window). `abs(current - mean) / 0` is undefined; the naive
  fix of treating it as "infinite score whenever current != mean" flags any
  tiny, harmless deviation as a maximum-severity anomaly. `mad_detector` has
  the same failure mode when the median absolute deviation collapses to 0
  (happens whenever >=50% of the window shares one value).
- Seasonality: a flat mean/std over a mixed Mon-Sun window bakes weekday and
  weekend traffic into one baseline. A normal Saturday can look like a huge
  drop against a Mon-Fri-heavy mean, and a normal Monday can look like a
  spike -- both are false positives caused by comparing across the wrong
  segment, not real incidents.
- Outliers: mean and std are not robust -- a single past spike (a flash
  sale, a backfill) drags the mean up and inflates std, which then *raises*
  the bar for detecting the next real anomaly (a smaller true drop no longer
  clears `threshold * std`). Median/MAD are far less sensitive to a handful
  of extreme points in the window.
"""
from __future__ import annotations

from typing import Any, Iterable

import numpy as np


def zscore_detector(current: float, history: Iterable[float], threshold: float = 3.0) -> dict[str, Any]:
    values = np.asarray(list(history), dtype=float)
    if values.size < 3:
        return {"is_anomaly": False, "score": 0.0, "method": "zscore", "reason": "insufficient_history"}
    mean = float(np.mean(values))
    std = float(np.std(values))
    if std == 0:
        score = float("inf") if float(current) != mean else 0.0
    else:
        score = abs(float(current) - mean) / std
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "zscore",
        "reason": f"mean={mean:.3f}, std={std:.3f}, threshold={threshold}",
    }


def _zero_spread_score(current: float, center: float) -> float:
    """Score for a window with zero spread (std or MAD both collapse to 0).

    Only flag a real, non-trivial deviation -- not floating point noise --
    instead of either (a) declaring every nudge an infinite-severity anomaly
    or (b) silently saying "never anomalous" just because the window happened
    to be perfectly flat.
    """
    tolerance = max(1e-9, abs(center) * 1e-6)
    return 0.0 if abs(current - center) <= tolerance else float("inf")


def mad_detector(current: float, history: Iterable[float], threshold: float = 3.5) -> dict[str, Any]:
    values = np.asarray(list(history), dtype=float)
    if values.size < 3:
        return {"is_anomaly": False, "score": 0.0, "method": "mad", "reason": "insufficient_history"}
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad == 0:
        score = _zero_spread_score(float(current), median)
        reason = f"median={median:.3f}, mad=0 (degenerate window), threshold={threshold}"
    else:
        score = 0.6745 * abs(float(current) - median) / mad
        reason = f"median={median:.3f}, mad={mad:.3f}, threshold={threshold}"
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "mad",
        "reason": reason,
    }


def _ewma_stats(values: np.ndarray, alpha: float = 0.35) -> tuple[float, float]:
    """Exponentially-weighted mean/std -- more weight on recent history.

    Captures a gradual trend (organic growth, a slow ramp-down) that a flat
    mean/median over the whole window would not track, which otherwise
    causes false positives on the healthy side of the trend.
    """
    if values.size == 0:
        return 0.0, 0.0
    weights = (1 - alpha) ** np.arange(values.size - 1, -1, -1)
    weights = weights / weights.sum()
    mean = float(np.sum(weights * values))
    variance = float(np.sum(weights * (values - mean) ** 2))
    return mean, variance**0.5


def _robust_score(current: float, values: np.ndarray) -> tuple[float, float, float]:
    """(score, median, mad) via median/MAD robust z-score."""
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    score = 0.6745 * abs(current - median) / mad if mad > 0 else _zero_spread_score(current, median)
    return score, median, mad


def auto_detector(
    current: float,
    history: Iterable[float],
    *,
    threshold: float = 3.0,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Context-aware detector: same-weekday baseline + robust MAD + EWMA trend,
    cross-checked against the full history before trusting a narrow segment.

    `history` is the full recent window (mixed weekdays), used both for the
    EWMA trend baseline and as a global robust baseline. If
    `context["same_segment_history"]` is present (e.g. the last N values for
    the same day-of-week) and long enough, it becomes the primary view for a
    robust median/MAD comparison -- but a same-weekday segment is often only
    5-8 points, which can look extremely tight (small MAD) by chance alone
    and then call a perfectly normal value a huge anomaly. So a segment-only
    verdict is only trusted when it is corroborated by *either* the global
    spread or the EWMA trend also finding the value unusual; a value that's
    only extreme against the narrow segment but unremarkable against
    everything else recently observed is not flagged (still reported in the
    score/reason for visibility, just not actioned as an anomaly).
    """
    context = context or {}
    history_values = np.asarray(list(history), dtype=float)

    segment_values = np.asarray([], dtype=float)
    raw_segment = context.get("same_segment_history")
    if raw_segment is not None:
        segment_values = np.asarray(list(raw_segment), dtype=float)
    used_segment = segment_values.size >= 5

    if not used_segment and history_values.size < 3:
        return {"is_anomaly": False, "score": 0.0, "method": "auto", "reason": "insufficient_history"}

    current = float(current)
    ewma_mean, ewma_std = _ewma_stats(history_values)
    trend_score = abs(current - ewma_mean) / ewma_std if ewma_std > 0 else 0.0
    trend_flag = trend_score > threshold

    if used_segment:
        segment_score, median, mad = _robust_score(current, segment_values)
        segment_flag = segment_score > threshold

        if history_values.size >= 3:
            global_score, g_median, g_mad = _robust_score(current, history_values)
        else:
            global_score, g_median, g_mad = 0.0, median, mad
        global_flag = global_score > threshold

        # Trust the narrow segment only when a broader view agrees.
        is_anomaly = segment_flag and (global_flag or trend_flag)
        score = segment_score
        basis = "same_weekday" if is_anomaly or not segment_flag else "same_weekday(uncorroborated)"
        reason = (
            f"segment_median={median:.3f}, segment_mad={mad:.3f}, segment_score={segment_score:.3f}, "
            f"global_median={g_median:.3f}, global_score={global_score:.3f}, "
            f"ewma_mean={ewma_mean:.3f}, trend_score={trend_score:.3f}, basis={basis}, threshold={threshold}"
        )
    else:
        score, median, mad = _robust_score(current, history_values)
        is_anomaly = (score > threshold) or trend_flag
        score = max(score, trend_score)
        reason = (
            f"median={median:.3f}, mad={mad:.3f}, ewma_mean={ewma_mean:.3f}, ewma_std={ewma_std:.3f}, "
            f"basis=raw_history, threshold={threshold}"
        )

    known_event = context.get("known_event")
    if known_event and is_anomaly:
        # A flagged/expected event (planned promo, holiday, maintenance
        # window) explains the shift, so it isn't actionable as a data
        # anomaly -- suppress the verdict but keep the score/reason above
        # for diagnostics (an on-call engineer can still see it moved).
        is_anomaly = False
        reason += f", suppressed_by_known_event={known_event}"

    return {
        "is_anomaly": bool(is_anomaly),
        "score": float(score),
        "method": f"auto:mad+ewma({'same_weekday' if used_segment else 'raw_history'})",
        "reason": reason,
    }


def detect_anomaly(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    threshold: float = 3.0,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable lab API.

    - `zscore`: basic mean/std z-score.
    - `mad`: robust median/MAD z-score.
    - `auto`: context-aware -- same-weekday baseline (via
      `context["same_segment_history"]`) + median/MAD + EWMA trend, see
      `auto_detector`.
    """
    if method == "mad":
        return mad_detector(current, history, threshold=threshold)
    if method == "zscore":
        return zscore_detector(current, history, threshold=threshold)
    if method == "auto":
        return auto_detector(current, history, threshold=threshold, context=context)
    raise ValueError(f"Unsupported method: {method}")
