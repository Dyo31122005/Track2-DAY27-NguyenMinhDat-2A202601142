#!/usr/bin/env python3
"""Great Expectations Core 1.21 flow for the `orders` dataset.

Builds a reusable Expectation Suite (driven by contracts/orders_contract.yaml,
so GX and the deterministic src/contract_validator stay in sync) wrapped in a
ValidationDefinition + Checkpoint. Each expectation carries GX's native
``severity`` (critical/warning/info, see FailureSeverity), and the Checkpoint
runs a custom QuarantineAction that automatically splits rows failing a
critical expectation into a side table (data/quarantine/orders_quarantine.csv)
instead of letting the whole batch fail closed with no remediation path.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import great_expectations as gx
    from great_expectations.checkpoint import actions
    from great_expectations.core.result_format import ResultFormat
except ImportError as exc:  # friendlier classroom failure
    raise SystemExit("great_expectations is not installed. Run: pip install -r requirements.txt") from exc

from src.contract_validator import load_contract

QUARANTINE_PATH = ROOT / "data" / "quarantine" / "orders_quarantine.csv"


def build_suite(context: "gx.data_context.AbstractDataContext", contract: dict[str, Any]) -> "gx.ExpectationSuite":
    """Translate contracts/orders_contract.yaml into GX expectations.

    Building the suite from the same contract file the deterministic
    validator reads keeps both layers honest about what "critical" means for
    this dataset instead of maintaining two divergent rule sets.
    """
    suite = gx.ExpectationSuite(name="orders_suite")
    columns = contract.get("columns", {})

    for column, rules in columns.items():
        severity = rules.get("severity", "warning")

        if rules.get("required"):
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToNotBeNull(column=column, severity=severity)
            )
        if rules.get("unique"):
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeUnique(column=column, severity=severity)
            )
        accepted = rules.get("accepted_values")
        if accepted is not None:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeInSet(column=column, value_set=accepted, severity=severity)
            )
        if "min" in rules or "max" in rules:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeBetween(
                    column=column,
                    min_value=rules.get("min"),
                    max_value=rules.get("max"),
                    severity=severity,
                )
            )

    return context.suites.add(suite)


class QuarantineAction(actions.ValidationAction):
    """Custom GX action: on a critical failure, quarantine the offending rows.

    Uses each failed expectation's `unexpected_index_list` (requires
    result_format=COMPLETE) to build the set of bad row positions, unions
    them across every expectation whose severity is critical, and writes
    those rows out to a side table so the rest of the batch can still flow.
    """

    type: str = "quarantine"
    # Plain strings (not a live DataFrame) so the action stays JSON-serializable
    # when GX persists the Checkpoint config to its store.
    orders_path: str
    output_path: str

    def run(self, checkpoint_result, action_context=None) -> dict:
        summary: dict[str, Any] = {}
        df = pd.read_csv(self.orders_path)

        for key, result in checkpoint_result.run_results.items():
            max_severity = result.get_max_severity_failure()
            summary[str(key)] = {
                "success": result.success,
                "max_severity_failure": max_severity.value if max_severity else None,
            }

            if max_severity is None or max_severity.value != "critical":
                continue

            bad_positions: set[int] = set()
            for expectation_result in result.results:
                cfg = expectation_result.expectation_config
                if expectation_result.success:
                    continue
                if getattr(cfg, "severity", None) is None or cfg.severity.value != "critical":
                    continue
                bad_positions.update(expectation_result.result.get("unexpected_index_list") or [])

            if not bad_positions:
                continue

            mask = df.index.isin(bad_positions)
            quarantined = df.loc[mask].copy()
            quarantined["quarantine_reason"] = "gx_critical_expectation_failure"
            out_path = Path(self.output_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            quarantined.to_csv(out_path, index=False)
            summary[str(key)]["quarantined_rows"] = int(mask.sum())
            summary[str(key)]["quarantine_path"] = self.output_path
            print(
                f"[QuarantineAction] critical failure detected -> quarantined "
                f"{int(mask.sum())} row(s) to {self.output_path}"
            )

        return summary


def main() -> None:
    orders_path = ROOT / "data" / "incoming" / "orders.csv"
    df = pd.read_csv(orders_path)
    contract = load_contract(ROOT / "contracts" / "orders_contract.yaml")

    context = gx.get_context()
    data_source = context.data_sources.add_pandas("orders_pandas")
    asset = data_source.add_dataframe_asset(name="orders_dataframe")
    batch_definition = asset.add_batch_definition_whole_dataframe("whole_orders")

    suite = build_suite(context, contract)
    validation_definition = context.validation_definitions.add(
        gx.ValidationDefinition(name="orders_validation", data=batch_definition, suite=suite)
    )

    quarantine_action = QuarantineAction(
        name="quarantine_critical", orders_path=str(orders_path), output_path=str(QUARANTINE_PATH)
    )
    checkpoint = context.checkpoints.add(
        gx.Checkpoint(
            name="orders_checkpoint",
            validation_definitions=[validation_definition],
            actions=[quarantine_action],
            result_format=ResultFormat.COMPLETE,
        )
    )

    result = checkpoint.run(batch_parameters={"dataframe": df})

    print(f"{'expectation':<40}{'severity':<10}{'success':<8}")
    for run_result in result.run_results.values():
        for expectation_result in run_result.results:
            cfg = expectation_result.expectation_config
            severity = cfg.severity.value if getattr(cfg, "severity", None) else "n/a"
            print(f"{cfg.type:<40}{severity:<10}{str(expectation_result.success):<8}")

        max_severity = run_result.get_max_severity_failure()
        print(f"\nmax_severity_failure: {max_severity.value if max_severity else None}")

    print("\nCheckpoint result:", "PASS" if result.success else "FAIL")
    print("(quarantine outcome, if any, is logged above by QuarantineAction itself)")


if __name__ == "__main__":
    main()
