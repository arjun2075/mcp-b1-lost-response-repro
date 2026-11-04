# D01 independent CI evidence — November 4, 2026 UTC

- PR: https://github.com/arjun2075/mcp-b1-lost-response-repro/pull/7
- GitHub Actions run: https://github.com/arjun2075/mcp-b1-lost-response-repro/actions/runs/41777526180
- Job: 126401252570 — success
- Result artifact: https://github.com/arjun2075/mcp-b1-lost-response-repro/actions/runs/41777526180/artifacts/13297606958
- Environment: Python 3.12.15, SQLite 3.46.1, Ubuntu Linux
- Unit suite: 6/6 passing (0.976 seconds)
- Fresh-worker scenario report: 6/6 passing

## Inspected artifact

The archived d01-results.json contains a separate subprocess result for each step, including explicit hard-process exit codes, recovery statuses, independent provider effect counts and source SHA-256 digests.

| Scenario | Observed restart behavior | Effect count |
| --- | --- | ---: |
| Commit before result persistence | Hard exit 37; subsequent worker reports unknown and never redispatches | 1 |
| Authoritative correlation | Hard exit 37; independent exact-scoped lookup recovers and retains original receipt | 1 |
| Reservation before effect | Hard exit 36; recovery and empty oracle lookup remain unknown, with no dispatch | 0 |
| Completed receipt persistence | A new process returns the original receipt | 1 |
| Deliberately unsafe control | Hard exit 37 before reservation; retry dispatches again | 2 |
| Scope, conflict and disclosure | Foreign principal denied, changed request conflicts, receipt withheld; separate operation admitted | 2 (distinct operations) |

Exact source digests in the result artifact:

- durable_restart.py — 24ff503f9f13ebd2da45e078badaf0436550a7d14e5117da6a2d462c3401c6e5
- test_durable_restart.py — 03b618e26496eff92ba1c5b0a98f4382c518e622eb1479554405e0a603d566e1
- run_durable_cases.py — d226488fe8f6271f02e3d4b82361464f48ca19dee4664f2ab0e3c1f7f4206307

**Interpretation:** The locally committed SQLite admission record survives a hard worker process exit and blocks a duplicate effect in this synthetic test. The unsafe control intentionally duplicates. The result does not prove disk-power-loss persistence, production provider reconciliation, cross-replica coordination, or exactly-once remote payments.
