# C01: overlapping retry admission (experimental)

This increment adds a deterministic concurrency test to the merged [retry-recovery fixture](README.md). It does **not** modify the earlier sequential recovery.Engine, the signed-approval worker, or the retention policy. It models a synthetic refund with an independent append-only effect ledger.

## Test boundary

Two tool calls overlap while a synthetic provider commit is deliberately held by an event. There are **no sleeps** or timing guesses to open a race. The controller releases the effect only after verifying the second request has been observed. The reservation-first strategy protects the lookup/reservation critical section with an anyio.Lock and does not hold that lock across the provider effect. A second caller sees unknown while the original operation is pending. After completion, recovery yields the original receipt.

The unsafe negative control checks an empty store before awaiting the same provider gate and writes its record only after dispatch. Both overlapping calls therefore see an empty store and both commit an effect. A pass on this control means the duplicate was **observed**, not prevented.

| Case | Expected effect count | Required observation |
| --- | ---: | --- |
| C01 reserved same-identity retry | 1 | First call pending; second returns unknown, starts no second dispatch; recovery returns original receipt |
| C01 deliberately unlocked control | 2 | Both requests pass the check before either effect and independently commit |
| C01 post-commit error | 1 | No receipt is stored; both follower and later recovery remain unknown, never redispatch |
| C01 same identity, changed arguments | 1 | Second returns conflict without starting a second dispatch |
| C01 intentionally distinct operation identities | 2 | Independently admitted operations each commit one effect |

An additional unit regression checks that a different authenticated principal or denied disclosure never receives the original receipt. Caller identity is **harness-controlled**, not established by MCP or PIC.

## Reproduction

From experimental/retry-recovery with dependencies installed from the repository root (mcp==2.2.0, anyio, mcp-types):

    python -m unittest -v test_concurrency
    python run_concurrency_cases.py --output local-results/c01-results.json

The first command exercises six event-controlled concurrent task scenarios and uses no MCP dependency. The second exercises five overlapping cases through real MCP 2.2.0 client/server memory streams and independently checks JSON-RPC IDs, effect counts, gate-state evidence, and client-visible outcomes. It refuses optimized Python because it uses assertions. The JSON report includes exact source SHA-256 digests, versions and per-case observations.

## Non-claims and follow-ups

The lock is **in-process only**. There is no durable reservation, distributed transaction, concurrency across replicas or threads, crash/restart recovery, actual payment/notification provider, independent admission service, production authentication, or deployed MCP retry semantics. This tests a candidate application-level policy, not a new normative MCP requirement.

The previous sequential engine and retention implementation are not made safe for concurrent RPC dispatch by this separate fixture. The future durable-restart increment requires storage transactions or an externally enforced idempotency mechanism and an explicit test at commit-before-receipt persistence.
