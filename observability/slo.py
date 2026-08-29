from __future__ import annotations

from typing import Any


def calculate_slo(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    if not 0 < target < 1:
        raise ValueError("target must be between 0 and 1 (exclusive)")
    if bad_events < 0 or total_events < 0 or bad_events > total_events:
        raise ValueError("invalid event counts")
    allowed_bad_rate = 1.0 - target
    if total_events == 0:
        return {
            "target": target,
            "actual_bad_rate": 0.0,
            "allowed_bad_rate": allowed_bad_rate,
            "burn_rate": 0.0,
            "remaining_error_budget_fraction": 1.0,
            "breached": False,
        }
    actual_bad_rate = bad_events / total_events
    burn_rate = actual_bad_rate / allowed_bad_rate
    consumed_fraction = min(1.0, actual_bad_rate / allowed_bad_rate)
    return {
        "target": target,
        "actual_bad_rate": actual_bad_rate,
        "allowed_bad_rate": allowed_bad_rate,
        "burn_rate": burn_rate,
        "remaining_error_budget_fraction": max(0.0, 1.0 - consumed_fraction),
        "breached": bool(actual_bad_rate > allowed_bad_rate),
    }


def evaluate_multiwindow_burn(
    *,
    short_window_burn: float,
    long_window_burn: float,
    policy: str = "google_sre",
    critical_threshold: float = 14.4,
    warning_threshold: float = 6.0,
) -> dict[str, Any]:
    """Two-window burn-rate policy, modeled on Google's SRE workbook official
    paging table (https://sre.google/workbook/alerting-on-slos/): pair a
    short window (e.g. 5m/30m) with a longer one (e.g. 1h/6h) over the same
    metric. The workbook defines **two paging tiers**, not one page + one
    silent ticket tier:

    - critical (page): BOTH windows >= 14.4x -- a fast burn that would
      exhaust 2% of a 30-day error budget within 1h if sustained.
    - warning (page): BOTH windows >= 6.0x (but not both >= 14.4x) -- a
      slower sustained burn (5% of budget in 6h). Still a real,
      budget-threatening burn, just lower urgency than the fast tier -- the
      workbook pages for this too, it isn't a "maybe, file a ticket" case.
    - info / no page: everything else. Requiring *both* windows to agree at
      a tier is exactly what tells a transient spike (short window high,
      long window never moved -- or has already recovered) apart from a
      genuinely sustained burn; a short-only or long-only signal does not
      page at any tier.
    """
    if policy != "google_sre":
        raise ValueError(f"Unsupported policy: {policy}")

    short = float(short_window_burn)
    long_ = float(long_window_burn)

    if short >= critical_threshold and long_ >= critical_threshold:
        page, severity = True, "critical"
        reason = f"sustained fast burn: short={short:.2f} and long={long_:.2f} both >= {critical_threshold} -- page now"
    elif short >= warning_threshold and long_ >= warning_threshold:
        page, severity = True, "warning"
        reason = (
            f"sustained burn: short={short:.2f} and long={long_:.2f} both >= {warning_threshold} "
            f"(below the {critical_threshold} fast-burn tier) -- page, lower urgency"
        )
    elif short >= warning_threshold:
        page, severity = False, "info"
        reason = f"transient spike: short={short:.2f} is high but long={long_:.2f} never sustained -- no page"
    elif long_ >= warning_threshold:
        page, severity = False, "info"
        reason = f"long window was elevated (long={long_:.2f}) but short window has recovered (short={short:.2f}) -- no page"
    else:
        page, severity = False, "info"
        reason = f"burn rate nominal: short={short:.2f}, long={long_:.2f}"

    return {
        "page": page,
        "severity": severity,
        "reason": reason,
        "short_window_burn": short,
        "long_window_burn": long_,
    }
