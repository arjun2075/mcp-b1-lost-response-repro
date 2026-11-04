# D01 — durable reservation across a hard process restart

This is a separate **experimental** increment for MCP issue #3394. It demonstrates one crash boundary using two independent SQLite databases: an application admission/receipt store and a synthetic provider effect oracle. The two databases do not share an atomic transaction. This is not a real PSP or external provider.

## Model and invariants

- Before dispatch, the application writes a scoped pending reservation (tenant, principal, tool, operation ID, semantic request) in a SQLite transaction committed using WAL and synchronous=FULL.
- The synthetic provider appends an effect/receipt to a *different* SQLite database. A worker can then terminate abruptly with os._exit(37) before the application persists the receipt.
- A new Python worker/process starts, reopens both database files and retries the same logical operation. It finds the pending reservation and must answer **unknown** without dispatching again.
- Separately invoked reconciliation reads the synthetic provider oracle using the exact authenticated scope and semantic request. One uniquely correlated result permits receipt recovery; an absent or ambiguous result remains **unknown** and cannot authorize redispatch.
- A deliberate unsafe control commits the external effect *without* storing any prior reservation. After the same hard exit, a fresh worker sees no record and repeats the effect (2 total). Passing that control means the expected duplicate occurred, not that the implementation is safe.

The provider-effect ledger is never pruned and does not share a transaction with the app store. The pending reservation survives the process exit. A hard process exit is not a hardware power-loss or torn-write test; WAL/FULL are configuration assumptions under the local filesystem.

## Executed scenarios

| Case | Crash point / behavior | Expected effects | Recovery |
| --- | --- | ---: | --- |
| D01A | Reserve, provider commits, hard-exit before app result persisted | 1 | Unknown; no redispatch |
| D01B | Same as D01A, then uniquely correlated authoritative provider lookup | 1 | Original receipt recovered and persisted |
| D01C | Hard-exit after reservation but before provider effect | 0 | Unknown; absence of provider row is not proof |
| D01D | Receipt persisted before process exits normally | 1 | Original receipt returned by new worker |
| D01E | Unsafe effect-before-reservation, hard-exit | 2 | New worker redispatches; expected counterexample |
| D01F | Context/semantic checks after restart | 2 including a distinct authorized operation | Foreign principal denied; changed request conflicts; disclosure denied |

D01F's second effect is a separately admitted operation, not a duplicate of the first.

## Reproduction

From experimental/retry-recovery, using Python 3.12+ and stdlib only:

    python -m unittest -v test_durable_restart
    python run_durable_cases.py --output local-results/d01-results.json

Each worker invocation is a new OS Python process through subprocess.run; the injected process exits with code 36 (after reservation) or 37 (after provider commit). The tests require these exact codes. Each case has fresh temporary SQLite databases and a separate effect counter. Reports include per-step command arguments and response/status, Python/SQLite versions and SHA-256 source digests.

The GitHub Actions workflow runs both commands on Python 3.12 and archives the structured result artifact.

## Important non-claims

- This does not prove exactly-once remote payment effects. It proves *no second dispatch for a retained pending operation* in this synthetic model; without authoritative correlated evidence the outcome remains unknown.
- It is not a production recovery service, real authentication, PIC verification, MCP transport scenario, distributed lock, or replica/thread safety proof. C01 overlap is tested separately in PR #6.
- The deliberately unsafe strategy is a negative control only. The safety claim requires the reservation to survive and remain consistent. No whole-disk loss, backup restoration, filesystem corruption, partial fsync or operator state deletion was simulated.
- The synthetic provider lookup is exact, strongly consistent and authoritative **by construction**. No actual PSP reconciliation API or eventual-consistency behavior was exercised.
- Records in this fixture are never pruned; retention policy still requires a separate admission-expiry design. No normative MCP retry or idempotency requirement is assumed.

Any upstream use should label the failure modes and oracle assumptions explicitly, rather than presenting a SQLite demonstration as a durable distributed-payment guarantee.
