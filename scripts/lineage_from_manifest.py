#!/usr/bin/env python3
"""Phase 4 (advanced): rebuild lineage from a real dbt run instead of the
hand-maintained data/baseline/lineage_graph.json, and compare the two.

Run after `make dbt` (needs dbt_project/target/manifest.json; run
`dbt docs generate` first too if you want the richer column-level graph via
catalog.json -- `make dbt` alone does not generate it).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.lineage import (  # noqa: E402
    build_openlineage_events,
    extract_dbt_column_graph,
    extract_dbt_dataset_graph,
    get_downstream_assets,
    load_graph,
    emit_openlineage_events,
)

MANIFEST = ROOT / "dbt_project" / "target" / "manifest.json"
CATALOG = ROOT / "dbt_project" / "target" / "catalog.json"
BASELINE = ROOT / "data" / "baseline" / "lineage_graph.json"
OPENLINEAGE_OUT = ROOT / "reports" / "openlineage_events.jsonl"


def main() -> None:
    if not MANIFEST.exists():
        raise SystemExit(f"{MANIFEST.relative_to(ROOT)} not found -- run `make dbt` first.")

    manifest_graph = extract_dbt_dataset_graph(MANIFEST)
    baseline_graph = load_graph(BASELINE)

    print("=== dataset lineage: manifest.json vs data/baseline/lineage_graph.json ===")
    for start in ("stg_orders", "stg_customers"):
        manifest_blast = get_downstream_assets(manifest_graph, start)
        baseline_blast = get_downstream_assets(baseline_graph, start)
        print(f"{start}:")
        print(f"  from manifest.json (real dbt DAG) : {manifest_blast}")
        print(f"  from baseline JSON (curated)      : {baseline_blast}")

    print(
        "\nNote: the manifest graph only knows what dbt built (seeds/models); "
        "the curated baseline JSON additionally covers non-dbt hops (BI "
        "dashboards, the RAG pipeline) -- so it's expected/correct that the "
        "curated graph reaches further downstream than the manifest graph."
    )

    if CATALOG.exists():
        column_graph = extract_dbt_column_graph(MANIFEST, CATALOG)
        print(f"\n=== column-level lineage ({len(column_graph)} source columns traced) ===")
        for source_col, targets in sorted(column_graph.items()):
            print(f"  {source_col} -> {targets}")
    else:
        print(f"\n{CATALOG.relative_to(ROOT)} not found -- run `dbt docs generate` for column-level lineage.")

    events = build_openlineage_events(manifest_graph)
    emit_openlineage_events(manifest_graph, OPENLINEAGE_OUT)
    print(f"\nWrote {len(events)} OpenLineage START/COMPLETE events to {OPENLINEAGE_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
