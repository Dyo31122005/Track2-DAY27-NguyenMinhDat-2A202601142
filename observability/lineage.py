"""Lineage: dataset-level blast radius, column-level lineage, and a manifest
parser that can rebuild the dataset graph straight from a dbt run instead of
relying on the hand-maintained data/baseline/lineage_graph.json.
"""
from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ASSET_RESOURCE_TYPES = {"model", "seed", "source"}


def load_graph(path: str | Path) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload["dataset_lineage"] if "dataset_lineage" in payload else payload


def _transitive_downstream(graph: dict[str, list[str]], start: str) -> list[str]:
    """Shared BFS: transitive downstream nodes in graph, excluding start."""
    seen = {start}
    q: deque[str] = deque([start])
    out: list[str] = []
    while q:
        node = q.popleft()
        for child in graph.get(node, []):
            if child not in seen:
                seen.add(child)
                out.append(child)
                q.append(child)
    return out


def get_downstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    """Return transitive downstream assets in BFS order, excluding start."""
    return _transitive_downstream(graph, start)


def get_column_downstream(column_graph: dict[str, list[str]], start_column: str) -> list[str]:
    """Return transitive downstream columns (e.g. 'stg_orders.amount_usd') in
    BFS order, excluding start_column. Same traversal as get_downstream_assets,
    just over a column-keyed graph instead of a dataset-keyed one.
    """
    return _transitive_downstream(column_graph, start_column)


def _friendly_names(manifest: dict[str, Any]) -> dict[str, str]:
    """Map every model/seed/source unique_id to its short, human name."""
    names: dict[str, str] = {}
    for unique_id, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") in _ASSET_RESOURCE_TYPES:
            names[unique_id] = node.get("name", unique_id)
    for unique_id, node in manifest.get("sources", {}).items():
        names[unique_id] = node.get("name", unique_id)
    return names


def extract_dbt_dataset_graph(manifest_path: str | Path) -> dict[str, list[str]]:
    """Dataset-level lineage straight from a dbt manifest's child_map.

    Only keeps model/seed/source nodes (drops test/unit_test nodes, which
    would otherwise pollute blast-radius output with things like
    'assert_nonnegative_revenue') and resolves unique_ids down to the same
    short names data/baseline/lineage_graph.json uses (e.g. 'stg_orders'),
    so the two graphs are directly comparable.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    names = _friendly_names(manifest)
    graph: dict[str, list[str]] = {}
    for parent_id, child_ids in manifest.get("child_map", {}).items():
        parent_name = names.get(parent_id)
        if parent_name is None:
            continue
        bucket = graph.setdefault(parent_name, [])
        for child_id in child_ids:
            child_name = names.get(child_id)
            if child_name is not None and child_name not in bucket:
                bucket.append(child_name)
    return graph


# --- Column-level lineage from compiled SQL -------------------------------
#
# dbt-core's manifest has no built-in column lineage. This is a deliberately
# scoped, best-effort SQL reader (not a general SQL parser): for each model's
# *final* top-level SELECT it matches each output column's expression against
# every direct parent's known columns by name. It correctly follows a rename
# through a simple cast (e.g. `cast(amount as double) as amount_usd` still
# links parent column `amount`), which is exactly the kind of edge
# data/baseline/lineage_graph.json has to declare by hand. It will *not*
# resolve multi-column expressions/aggregates to a single "true" source
# column (e.g. `count(*) as completed_order_rows` correctly yields no edge --
# there isn't one single source column to point at).

_SQL_COMMENT_RE = re.compile(r"--[^\n]*")
_TRAILING_ALIAS_RE = re.compile(r"(?i)\bas\s+([a-zA-Z_][\w]*)\s*$")
_TRAILING_IDENTIFIER_RE = re.compile(r"([a-zA-Z_][\w]*)\s*$")


def _strip_sql_comments(sql: str) -> str:
    return _SQL_COMMENT_RE.sub("", sql)


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split text on sep, ignoring any sep found inside parentheses."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and text.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_top_level_keyword(text: str, keyword: str, start: int = 0) -> int:
    """Index of the first `keyword` at paren-depth 0, on a word boundary, at/after `start`."""
    depth = 0
    lowered = text.lower()
    i = start
    n = len(keyword)
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif (
            depth == 0
            and lowered.startswith(keyword, i)
            and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_"))
            and (i + n >= len(text) or not (text[i + n].isalnum() or text[i + n] == "_"))
        ):
            return i
        i += 1
    return -1


def _final_select_list(sql: str) -> str | None:
    """The column list of the last top-level SELECT (i.e. after all CTEs)."""
    sql = _strip_sql_comments(sql)
    # Walk all top-level `select` occurrences; the last one is the model's
    # actual output projection (earlier ones are CTEs, which live inside
    # `name as ( select ... )` and are therefore at paren-depth > 0 already
    # -- except the very first CTE keyword itself sits at depth 0 right
    # before its opening paren, so we instead track "last select whose
    # matching column-list ends at a depth-0 FROM".
    positions = []
    idx = 0
    while True:
        idx = _find_top_level_keyword(sql, "select", idx)
        if idx == -1:
            break
        positions.append(idx)
        idx += 6
    if not positions:
        return None
    start = positions[-1] + 6
    from_idx = _find_top_level_keyword(sql, "from", start)
    if from_idx == -1:
        return None
    return sql[start:from_idx]


def _column_alias(expr: str) -> tuple[str, str]:
    """(expression_without_alias, output_column_name) for one select-list item."""
    expr = expr.strip()
    match = _TRAILING_ALIAS_RE.search(expr)
    if match:
        return expr[: match.start()].strip(), match.group(1)
    ident = _TRAILING_IDENTIFIER_RE.search(expr)
    return expr, (ident.group(1) if ident else expr)


def extract_dbt_column_graph(
    manifest_path: str | Path,
    catalog_path: str | Path | None = None,
) -> dict[str, list[str]]:
    """Best-effort column-level graph (see module note above) built from a
    dbt manifest's compiled SQL plus (optionally) a catalog.json for the
    full physical column list of each node -- run `dbt docs generate` to
    produce one; without it, only schema.yml-documented columns are known.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    nodes = manifest.get("nodes", {})
    names = _friendly_names(manifest)

    columns_by_id: dict[str, list[str]] = {
        uid: list(node.get("columns", {}).keys()) for uid, node in nodes.items() if uid in names
    }
    for uid, node in manifest.get("sources", {}).items():
        columns_by_id.setdefault(uid, list(node.get("columns", {}).keys()))

    if catalog_path is not None:
        catalog_file = Path(catalog_path)
        if catalog_file.exists():
            with open(catalog_file, "r", encoding="utf-8") as f:
                catalog = json.load(f)
            catalog_nodes = {**catalog.get("nodes", {}), **catalog.get("sources", {})}
            for uid, node in catalog_nodes.items():
                cols = list(node.get("columns", {}).keys())
                if cols:
                    columns_by_id[uid] = cols

    column_graph: dict[str, list[str]] = {}
    for uid, node in nodes.items():
        if node.get("resource_type") != "model":
            continue
        child_name = names.get(uid)
        sql = node.get("compiled_code") or node.get("raw_code") or ""
        select_list = _final_select_list(sql)
        if not child_name or not select_list:
            continue

        parent_ids = [p for p in node.get("depends_on", {}).get("nodes", []) if p in names]
        if not parent_ids:
            continue

        for raw_expr in _split_top_level(select_list, ","):
            if not raw_expr.strip():
                continue
            expr_body, out_col = _column_alias(raw_expr)
            for parent_id in parent_ids:
                parent_name = names[parent_id]
                for parent_col in columns_by_id.get(parent_id, []):
                    if re.search(rf"\b{re.escape(parent_col)}\b", expr_body):
                        key = f"{parent_name}.{parent_col}"
                        target = f"{child_name}.{out_col}"
                        bucket = column_graph.setdefault(key, [])
                        if target not in bucket:
                            bucket.append(target)

    return column_graph


# --- Minimal OpenLineage emission (optional bonus) -------------------------


def build_openlineage_events(
    dataset_graph: dict[str, list[str]],
    *,
    namespace: str = "data_reliability_lab",
    job_prefix: str = "build",
    producer: str = "https://github.com/data-reliability-lab/gx-dbt-lineage",
) -> list[dict[str, Any]]:
    """Build OpenLineage-shaped START/COMPLETE RunEvent pairs, one job per
    dataset that has at least one parent (i.e. one job per "build this model
    from its inputs" step). No network/backend required -- this returns
    plain dicts; write them with emit_openlineage_events or POST them to a
    real OpenLineage endpoint (e.g. Marquez) yourself.
    """
    parents: dict[str, list[str]] = {}
    for parent, children in dataset_graph.items():
        for child in children:
            parents.setdefault(child, []).append(parent)

    events: list[dict[str, Any]] = []
    run_time = datetime.now(timezone.utc).isoformat()
    for dataset, dataset_parents in parents.items():
        job_name = f"{job_prefix}_{dataset}"
        run_id = f"{job_name}-{abs(hash((job_name, run_time)))}"
        job = {"namespace": namespace, "name": job_name}
        run = {"runId": run_id}
        inputs = [{"namespace": namespace, "name": p} for p in dataset_parents]
        outputs = [{"namespace": namespace, "name": dataset}]
        for event_type in ("START", "COMPLETE"):
            events.append(
                {
                    "eventType": event_type,
                    "eventTime": run_time,
                    "producer": producer,
                    "schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/$defs/RunEvent",
                    "run": run,
                    "job": job,
                    "inputs": inputs if event_type == "COMPLETE" else [],
                    "outputs": outputs if event_type == "COMPLETE" else [],
                }
            )
    return events


def emit_openlineage_events(dataset_graph: dict[str, list[str]], output_path: str | Path, **kwargs: Any) -> int:
    """Write build_openlineage_events(...) as JSON Lines to output_path.

    Returns the number of events written.
    """
    events = build_openlineage_events(dataset_graph, **kwargs)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return len(events)
