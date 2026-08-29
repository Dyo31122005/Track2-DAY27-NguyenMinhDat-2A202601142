"""Contract validator used by the Data Reliability Game Day lab.

Implements:
- deterministic column checks (required/not-null/unique/accepted_values/range),
- explicit type validation (integer/number/string/datetime) instead of silently
  coercing with ``pd.to_numeric``,
- freshness validation driven by ``contract['freshness']``,
- severity classification (critical/warning/info) and a severity -> action
  mapping (block/quarantine/warn),
- an automatic quarantine helper that splits a dataframe into "clean" rows and
  a "quarantine" side table for rows that fail a critical check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# Severity -> pipeline action.
#   critical -> block:      stop the pipeline, do not let the data flow downstream.
#   warning  -> quarantine: split offending rows into a side table, let the rest through.
#   info     -> warn:       log it and continue, nothing blocks or gets split off.
_ACTION_BY_SEVERITY = {
    "critical": "block",
    "warning": "quarantine",
    "info": "warn",
}


def classify_action(severity: str, passed: bool) -> str:
    """Map a check's severity + pass/fail outcome to a pipeline action."""
    if passed:
        return "pass"
    return _ACTION_BY_SEVERITY.get(severity, "warn")


def overall_action(issues: list[dict[str, Any]]) -> str:
    """Reduce a list of issues to a single pipeline decision.

    block beats quarantine beats warn beats pass, i.e. one critical failure is
    enough to stop the pipeline even if other checks only warrant a warning.
    """
    actions = {classify_action(i.get("severity", "warning"), i.get("passed", True)) for i in issues}
    for candidate in ("block", "quarantine", "warn"):
        if candidate in actions:
            return candidate
    return "pass"


def _issue(
    check: str,
    *,
    column: str | None,
    severity: str,
    passed: bool,
    details: str,
) -> dict[str, Any]:
    # Keep this to exactly the shape documented in docs/STUDENT_API.md
    # (check/column/severity/passed/details) -- action classification is a
    # separate, derived concern (see classify_action/overall_action above),
    # not baked into every issue dict.
    return {
        "check": check,
        "column": column,
        "severity": severity,
        "passed": bool(passed),
        "details": details,
    }


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _type_invalid_mask(series: pd.Series, expected_type: str) -> pd.Series:
    """Return a boolean mask of values that do not match ``expected_type``.

    Unlike ``pd.to_numeric(..., errors="coerce")`` used alone, this explicitly
    reports which non-null values fail to match the declared type instead of
    silently turning them into NaN and losing the signal.
    """
    present = series.notna()
    expected_type = (expected_type or "").strip().lower()

    if expected_type in ("integer", "int"):
        numeric = pd.to_numeric(series, errors="coerce")
        non_numeric = present & numeric.isna()
        has_fraction = present & numeric.notna() & (numeric % 1 != 0)
        return non_numeric | has_fraction

    if expected_type in ("number", "float", "numeric"):
        numeric = pd.to_numeric(series, errors="coerce")
        return present & numeric.isna()

    if expected_type in ("datetime", "timestamp", "date"):
        parsed = pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
        return present & parsed.isna()

    if expected_type in ("string", "str", "text"):
        # Values arriving from CSV/JSON are already strings; the realistic
        # drift to catch here is the whole column silently becoming numeric.
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            return present.copy()
        return pd.Series(False, index=series.index)

    if expected_type in ("boolean", "bool"):
        return present & ~series.isin([True, False, "true", "false", "True", "False", 0, 1])

    # Unknown/unspecified declared type: nothing to check.
    return pd.Series(False, index=series.index)


def _freshness_issue(df: pd.DataFrame, contract: dict[str, Any]) -> dict[str, Any] | None:
    """Freshness check: updated_at vs contract['freshness']['max_delay_minutes'].

    Severity is always exactly what the contract declares (e.g. "warning")
    -- the *severity tiers* the lab asks for (critical/warning/info) come
    from different checks/columns declaring different severities in the
    YAML, not from this one check dynamically escalating based on how late
    the data is.
    """
    freshness = contract.get("freshness")
    if not freshness:
        return None

    column = freshness.get("column")
    max_delay = freshness.get("max_delay_minutes")
    severity = freshness.get("severity", "warning")
    if not column or column not in df.columns or max_delay is None:
        return None

    timestamps = pd.to_datetime(df[column], utc=True, errors="coerce", format="mixed")
    if timestamps.notna().sum() == 0:
        return _issue(
            "freshness",
            column=column,
            severity=severity,
            passed=False,
            details=f"No parseable timestamps in '{column}' to evaluate freshness.",
        )

    latest = timestamps.max()
    now = pd.Timestamp.now(tz="UTC")
    delay_minutes = max(0.0, (now - latest).total_seconds() / 60.0)
    passed = delay_minutes <= max_delay

    return _issue(
        "freshness",
        column=column,
        severity=severity,
        passed=passed,
        details=f"delay_minutes={delay_minutes:.2f}; max_delay_minutes={max_delay}",
    )


def validate_dataframe(df: pd.DataFrame, contract: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    columns = contract.get("columns") or contract.get("fields") or {}

    for column, rules in columns.items():
        rules = rules if isinstance(rules, dict) else {}
        severity = rules.get("severity", "warning")
        required = bool(rules.get("required", False))

        if column not in df.columns:
            if required:
                issues.append(
                    _issue(
                        "required_column",
                        column=column,
                        severity=severity,
                        passed=False,
                        details=f"Missing required column: {column}",
                    )
                )
            continue

        series = df[column]

        if required:
            null_count = int(series.isna().sum())
            issues.append(
                _issue(
                    "not_null",
                    column=column,
                    severity=severity,
                    passed=(null_count == 0),
                    details=f"null_count={null_count}",
                )
            )

        if rules.get("unique"):
            duplicate_count = int(series.duplicated(keep=False).sum())
            issues.append(
                _issue(
                    "unique",
                    column=column,
                    severity=severity,
                    passed=(duplicate_count == 0),
                    details=f"duplicate_rows={duplicate_count}",
                )
            )

        accepted = rules.get("accepted_values")
        if accepted is not None:
            invalid_mask = series.notna() & ~series.isin(accepted)
            invalid_count = int(invalid_mask.sum())
            issues.append(
                _issue(
                    "accepted_values",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; accepted={accepted}",
                )
            )

        expected_type = rules.get("type")
        if expected_type:
            invalid_mask = _type_invalid_mask(series, expected_type)
            invalid_count = int(invalid_mask.fillna(False).sum())
            issues.append(
                _issue(
                    "type",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"expected_type={expected_type}; invalid_count={invalid_count}",
                )
            )

        # Numeric range support. Only evaluated against values that already
        # parse as numbers; type drift itself is reported by the "type" check
        # above so this does not need to re-flag it.
        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(series, errors="coerce")
            invalid = pd.Series(False, index=series.index)
            if "min" in rules:
                invalid |= numeric < rules["min"]
            if "max" in rules:
                invalid |= numeric > rules["max"]
            invalid_count = int(invalid.fillna(False).sum())
            issues.append(
                _issue(
                    "range",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}",
                )
            )

        # Minimum string length (e.g. kb_contract.yaml's content: min_length: 20
        # -- catches empty/truncated documents that would otherwise pass a
        # bare not_null check).
        if "min_length" in rules:
            min_length = int(rules["min_length"])
            lengths = series.dropna().astype(str).str.len()
            too_short = int((lengths < min_length).sum())
            issues.append(
                _issue(
                    "min_length",
                    column=column,
                    severity=severity,
                    passed=(too_short == 0),
                    details=f"too_short_count={too_short}; min_length={min_length}",
                )
            )

    freshness_issue = _freshness_issue(df, contract)
    if freshness_issue is not None:
        issues.append(freshness_issue)

    return issues


def failed_issues(issues: list[dict[str, Any]], min_severity: str | None = None) -> list[dict[str, Any]]:
    failed = [i for i in issues if not i.get("passed", False)]
    if min_severity is None:
        return failed
    order = {"info": 0, "warning": 1, "critical": 2}
    threshold = order[min_severity]
    return [i for i in failed if order.get(i.get("severity", "warning"), 1) >= threshold]


def quarantine_dataframe(
    df: pd.DataFrame,
    contract: dict[str, Any],
    min_severity: str = "critical",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split ``df`` into (clean_rows, quarantined_rows).

    A row is quarantined when it individually fails a column-level check
    (not_null/unique/accepted_values/range/type) whose contract severity is
    at least ``min_severity`` (default: critical). This is the automatic
    quarantine mechanism: rows that would fail the pipeline are sent to a
    side table instead of silently flowing downstream, while rows that are
    fine keep moving.
    """
    order = {"info": 0, "warning": 1, "critical": 2}
    threshold = order.get(min_severity, 2)

    columns = contract.get("columns") or contract.get("fields") or {}
    bad_mask = pd.Series(False, index=df.index)
    reasons = pd.Series([[] for _ in range(len(df))], index=df.index)

    def mark(mask: pd.Series, reason: str) -> None:
        nonlocal bad_mask
        mask = mask.fillna(False)
        bad_mask = bad_mask | mask
        for idx in df.index[mask]:
            reasons.loc[idx].append(reason)

    for column, rules in columns.items():
        severity = rules.get("severity", "warning")
        if order.get(severity, 1) < threshold or column not in df.columns:
            continue

        series = df[column]

        if rules.get("required"):
            mark(series.isna(), f"{column}: null")

        if rules.get("unique"):
            mark(series.duplicated(keep=False), f"{column}: duplicate")

        accepted = rules.get("accepted_values")
        if accepted is not None:
            mark(series.notna() & ~series.isin(accepted), f"{column}: not_in_accepted_values")

        expected_type = rules.get("type")
        if expected_type:
            mark(_type_invalid_mask(series, expected_type), f"{column}: type_mismatch({expected_type})")

        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(series, errors="coerce")
            invalid = pd.Series(False, index=series.index)
            if "min" in rules:
                invalid |= numeric < rules["min"]
            if "max" in rules:
                invalid |= numeric > rules["max"]
            mark(invalid, f"{column}: out_of_range")

    clean_df = df.loc[~bad_mask].reset_index(drop=True)
    quarantined_df = df.loc[bad_mask].copy()
    quarantined_df["quarantine_reason"] = reasons.loc[bad_mask].apply("; ".join)
    quarantined_df = quarantined_df.reset_index(drop=True)
    return clean_df, quarantined_df
