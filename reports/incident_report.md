# Incident Report

> **Note on scope:** Phase 6's live instructor-provided mystery dataset was
> never injected in this session (`data/incoming/` stayed at healthy
> baseline throughout — verified via `make baseline` and `pytest
> tests_public`). Per the ground rules, no fault-generation script was read
> to compensate. Instead this report is a **Game Day drill**: the three
> public practice faults (`duplicate_pk`, `volume_drop`, `stale_kb`) run
> back-to-back and investigated purely from tool output, exactly as Phase 6
> asks for a real mystery. Every number below is real output copied from
> actual command runs in this session, not invented.

## Severity
P2 — one confirmed revenue-integrity risk (blocked before reaching the mart), one confirmed ingestion-volume incident, one support-content staleness gap found **and closed** during this drill.

## Summary
Three independent faults were drilled:
1. **`duplicate_pk`** — 3 duplicate `order_id` rows injected into `orders.csv`. Caught and blocked by both the contract validator and the GX checkpoint before reaching `fct_daily_revenue`.
2. **`volume_drop`** — incoming order volume cut to 150/600 rows (75% drop). Caught by the anomaly detector.
3. **`stale_kb`** — knowledge-base `published_at` timestamps shifted back 3h. **First run: not caught by any check** (`contracts/kb_contract.yaml` existed but nothing loaded/evaluated it). This was a real, standing detection gap, not a drill success — closed within the same investigation by wiring the existing generic contract validator to `kb_documents.jsonl` (see Root Cause/Mitigation); re-run after the fix catches it correctly.

## Detection
- Signals used: `src/contract_validator.py` (`unique`, `type`, `freshness`, now also run against `contracts/kb_contract.yaml`), `gx/validate_orders.py` (GX Checkpoint + severity), `observability/anomaly.py` (`detect_anomaly(method="auto")`), `make dbt` data tests, raw JSONL inspection.
- First observed: during this session's Phase-6 drill, 2026-08-29 (exact per-fault evidence below).

## Root Cause
1. **duplicate_pk**: simulated upstream re-delivery of the same order rows (`scripts/inject_fault.py duplicate_pk` appends the first 3 rows of `orders.csv` again) — a realistic at-least-once-delivery duplication.
2. **volume_drop**: simulated partial ingestion (`orders.csv` truncated to 25% of rows) — a realistic upstream extract/network failure mid-batch.
3. **stale_kb**: simulated a KB sync job stalling (`published_at` shifted -3h) — realistic background-worker lag. Root cause of the *detection gap* (now fixed): `contracts/kb_contract.yaml` was never loaded anywhere in the pipeline — `scripts/run_baseline.py` only computed a text-length anomaly signal for the KB dataset, which a timestamp shift does not move at all. `validate_dataframe()` is fully contract-driven and dataset-agnostic, so the fix was wiring, not new detection logic.

## Evidence

**1. duplicate_pk** (`python scripts/inject_fault.py duplicate_pk && make baseline`):
```
orders rows              : 603
contract failed checks   : 1
critical contract fails  : 1
```
`gx/validate_orders.py` output: `expect_column_values_to_be_unique  critical  False`, `max_severity_failure: critical`, checkpoint `FAIL`, and `QuarantineAction` wrote 6 rows (3 originals + 3 duplicates) to `data/quarantine/orders_quarantine.csv`.

**2. volume_drop** (`python scripts/inject_fault.py volume_drop && make baseline`):
```
orders rows              : 150
```
`row_count_anomaly`: `is_anomaly=True, score=5.53, method=auto:mad+ewma(same_weekday)`, reason: `segment_median=252.500, segment_mad=12.500, segment_score=5.531, global_median=576.000, global_score=13.365, ... basis=same_weekday, threshold=3.0` — flagged by both the same-weekday segment *and* the global cross-check (see Prevention item 3), so this is not a borderline call.

**3. stale_kb** (`python scripts/inject_fault.py stale_kb && make baseline`):

*Before the fix:*
```
contract failed checks   : 0
critical contract fails  : 0
KB length anomaly        : False
```
Raw JSONL inspection confirmed the fault was real (`published_at` ~3h in the past) but **nothing in `reports/latest_metrics.json` reflected it** — the pipeline reported fully healthy while serving stale policy content to the support bot.

*After wiring `contracts/kb_contract.yaml` into `scripts/run_baseline.py` (reusing `validate_dataframe()` against `kb_documents.jsonl`, same function used for orders):*
```
KB contract fails        : 1 (0 critical)
  KB issue                : freshness column=published_at severity=warning delay_minutes=190.01; max_delay_minutes=60
```
`delay_minutes=190.01` correctly exceeds `max_delay_minutes=60` from the contract. Severity is `warning` (as declared in the YAML, unescalated) — informative enough to act on without falsely reading as pipeline-blocking.

## Blast Radius
Traced with `observability.lineage.get_downstream_assets` against `data/baseline/lineage_graph.json`:
```text
stg_orders        -> fct_daily_revenue -> ceo_revenue_dashboard      (duplicate_pk, volume_drop)
kb_documents       -> kb_active_docs -> rag_index -> support_agent   (stale_kb)
```
duplicate_pk was quarantined before reaching `fct_daily_revenue`, so its blast radius was contained at the staging boundary. volume_drop's reduced row count propagates straight through to `ceo_revenue_dashboard` (a real, uncontained revenue-reporting impact — 150 rows worth of revenue instead of ~600). stale_kb's blast radius is the full RAG chain down to `support_agent` — customers could receive an outdated refund-policy answer; this drill's fix means that path is now observable (`kb_failed_checks` in the report), though nothing pages on it yet at `warning` severity (Prevention item 2).

## Mitigation
1. duplicate_pk: already contained — `QuarantineAction` (GX Checkpoint) and `quarantine_dataframe()` (contract validator) both split offending rows to a side table automatically; no manual action needed beyond reviewing `data/quarantine/orders_quarantine.csv` and re-ingesting deduplicated rows upstream.
2. volume_drop: page on-call per the burn-rate policy (`evaluate_multiwindow_burn`), pause the CEO dashboard refresh until the extract is confirmed complete, re-run the upstream extract for the missing 75%.
3. stale_kb: **implemented during this drill** — `contracts/kb_contract.yaml` is now loaded and validated every `make baseline` run (`scripts/run_baseline.py`), reporting `kb_failed_checks`/`kb_critical_failures`; a manual KB-sync-worker restart is still the actual remediation once the check fires, but the pipeline no longer reports "healthy" while serving stale content.

## Recovery
- `python scripts/reset_lab.py` restores a clean, freshly-timestamped baseline.
- Re-verified with `python scripts/run_baseline.py` and `pytest tests_public -q` → 10/10 passed, `contract failed checks: 0`, `critical contract fails: 0`, `KB contract fails: 0`.

## Verification
- [x] Contract healthy (`critical contract fails: 0` after reset)
- [x] KB contract healthy (`KB contract fails: 0` after reset — new check, verified both healthy-passes-clean and stale-triggers-correctly)
- [x] dbt tests healthy (`make dbt` → 20/20 PASS, see Phase 2 work)
- [x] anomaly returned to expected range (`row_count_anomaly.is_anomaly: False` after reset)
- [x] SLO healthy / budget understood (`slo_status(0.995, 0, N)` → `breached: False`)
- [x] downstream output verified for stale_kb (now checkable — was the point of Prevention item 1 below, closed in this drill)

## Prevention / Action Items
| Action | Owner | Deadline | Why |
|---|---|---|---|
| ~~Wire `contracts/kb_contract.yaml` into an actual freshness check on `data/incoming/kb_documents.jsonl`~~ **Done this drill** (`scripts/run_baseline.py` now calls `validate_dataframe()` against it) | Data Platform | closed 2026-08-29 | stale_kb was completely silent before this fix — the only fault of the three with zero detection coverage |
| Route `kb_failed_checks` into the SLO/alerting path the same way `critical_contract_failures` already is (currently only `kb_critical_failures` feeds `contract_slo`; `warning`-severity KB issues are visible in the report but don't page anyone) | SRE Team | 2026-09-05 | A `warning`-severity freshness breach (like this drill's) should still be actionable, not just observable in a JSON file nobody is paged to read |
| Block ingestion automatically (not just quarantine) when a `critical`-severity contract check fails, before any partial-batch load is possible | Data Platform | 2026-09-08 | volume_drop currently only pages after the fact; catching a mid-batch truncation before it lands would shrink the blast radius further |
| Keep the anomaly detector's global-spread cross-check (added this session) under regression test | SRE Team | 2026-09-10 | Without it, `auto` mode false-positived on every healthy weekend baseline (segment-only score 18.75, see `observability/anomaly.py::auto_detector`) — a real alert-fatigue risk that would have desensitized on-call to the real volume_drop signal |
