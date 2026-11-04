# C01 execution evidence — 2026-11-03 PST / 2026-11-04 UTC

GitHub Actions run: https://github.com/arjun2075/mcp-b1-lost-response-repro/actions/runs/41776400368

Run ID: 41776400368. Workflow job 126397676024 concluded **success** on Python 3.12.15, with mcp==2.2.0 and mcp-types==2.2.0. GitHub artifact 13296614308 (`c01-results`) contains the complete machine-readable, per-request results and source digests: https://github.com/arjun2075/mcp-b1-lost-response-repro/actions/runs/41776400368/artifacts/13296614308

- Six local event-controlled unit tests passed before upload and again in CI: `Ran 6 tests ... OK`.
- Five real-MCP in-memory stream scenarios passed in CI: `5/5 overlapping real-MCP scenarios passed`.
- Original source SHA-256:
  - concurrency.py: `89ed4f0c0f02903164aa88b83d5a0f16bbb8c2430860aa81165de2951a1da0c6`
  - run_concurrency_cases.py: `03283098a4ac8f4532e265e0169cc2e7e65c39d2ee508f11b618b09e86dcb716`

## Inspected run artifact

| Case | Provider dispatch attempts | Committed effects | First / second / recovery result |
| --- | ---: | ---: | --- |
| C01_reserved_overlapping_retry | 1 | 1 | completed / unknown / completed |
| C01_unlocked_check_then_act_counterexample | 2 | 2 | completed / completed / completed |
| C01_commit_then_error_unknown | 1 | 1 | execution_error / unknown / unknown |
| C01_inflight_changed_arguments_conflict | 1 | 1 | completed / conflict / completed |
| C01_independent_operation_positive_control | 2 | 2 | completed / completed / completed |

All five cases recorded zero effects before the release gate and three distinct JSON-RPC `tools/call` IDs. The safe overlapping case recorded the second response while the first commit was still held; the unsafe control had two provider dispatches already started before release.

For the unsafe control, **passing means the expected duplicate effect occurred**; it is not a claim of safety. The experiment does not establish durable restart recovery, cross-process/multi-replica exclusion, production provider semantics, authentication, or normative MCP conformance.
