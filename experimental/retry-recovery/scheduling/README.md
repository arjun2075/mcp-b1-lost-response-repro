# Scheduling fixture: rows S01-S03

Contributed by @Majny for the retry-recovery matrix in [../README.md](../README.md).
Synthetic and in-process only. Based on a meeting-scheduling MCP server, sanitized: no
real data, prompts or provider calls.

## Tool

`reschedule_meeting(meeting_id, start, timezone, operation_id?)`. Its effect is one
notification per attendee (3 attendees), appended to a synthetic ledger. The ledger is the
effect oracle: the count reported below is the number of notification batches in it, and
every row is checked. The dedup store is a separate dict and never touches the ledger.

Two dedup key strategies:

- **argument key:** digest of principal, tool name and semantic arguments
  (`meeting_id`, `start`, `timezone`). The server can derive it without any client help.
- **operation id:** caller-supplied `operation_id`, scoped to principal and tool.

Two store policies:

- **success-only:** store a result only after the tool returns successfully. A failed
  attempt leaves no record.
- **record-first:** record the operation as reserved before the effect, mark it completed
  after. A retry that finds a reserved record is not dispatched; it gets "outcome unknown".

## Rows

Per attempt: dispatches, effect delta (notification batches added to the ledger) and the
status the client saw. Both attempts are checked against these sequences.

| ID | Setup / fault | Key | Store | Intended | Observed | Dispatches | Effect deltas | Client statuses | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S01 | Lost response after notifications committed; one retry, same arguments | argument | record-first | 1 | 1 | 1, 0 | 1, 0 | timeout, replayed batch-1 | safe |
| S01 | same | operation id (same on retry) | record-first | 1 | 1 | 1, 0 | 1, 0 | timeout, replayed batch-1 | safe |
| S02 | No fault; identical arguments sent again on purpose as a new operation | argument | record-first | 2 | 1 | 1, 0 | 1, 0 | committed batch-1, replayed batch-1 | expected counterexample |
| S02 | same | operation id (new id for the repeat) | record-first | 2 | 2 | 1, 1 | 1, 1 | committed batch-1, committed batch-2 | safe |
| S03 | Notifications committed, then an error before the result; one retry | argument | success-only | 1 | 2 | 1, 1 | 1, 1 | injected error, committed batch-2 | expected counterexample |
| S03 | same | operation id | success-only | 1 | 2 | 1, 1 | 1, 1 | injected error, committed batch-2 | expected counterexample |
| S03 | same | argument | record-first | 1 | 1 | 1, 0 | 1, 0 | injected error, "outcome unknown" | safe |
| S03 | same | operation id | record-first | 1 | 1 | 1, 0 | 1, 0 | injected error, "outcome unknown" | safe |

What the rows show:

- **S01:** both keys survive a lost-response retry, so both absorb it.
- **S02:** an argument-derived key cannot tell a retry from an intentional repeat with
  identical arguments. It coalesces the repeat and the second batch is never sent. Only a
  caller-supplied operation identity separates the two.
- **S03:** caching only successful results does not make a failed attempt safe to repeat
  when the effect committed before the error; the key strategy does not matter. Recording
  the operation before the effect closes this in-process, at the cost of an unknown outcome
  on retry: the fixture does not claim the notifications were delivered.

"Expected counterexample" rows assert the unsafe count on purpose, like R04.

Mapping to [ryjen's table in #3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6254309502): S01 ≈ A/B, S02 = D, S03 (committed, then an error) is not in that table; C and E are not covered.

## Oracle

Each case fails, with the reason in the Check column, unless all of these hold:

- **S01 drop:** the relay drops only attempt 1's `tools/call` response, and only if it is
  a successful `JSONRPCResponse` (not a `JSONRPCError`, not a tool error result) sent after
  exactly one dispatch and one complete three-attendee batch, naming that batch. Otherwise
  it refuses and forwards the response.
- **Timeouts:** a caught `MCPError` counts as a timeout only on S01 attempt 1, with code
  `REQUEST_TIMEOUT` and exactly the SDK's message `Request 'tools/call' timed out`, after the
  relay recorded the drop of that attempt's JSON-RPC id and forwarded no reply for it. Any
  other exception from `call_tool`, `MCPError` or not, fails the case.
- **Per attempt:** status, dispatches, effect delta and batch id equal the sequence above.
  A replay must equal the result it replays (on S01, the dropped one). S03 attempt 1 must be
  exactly the injected post-commit error; record-first attempt 2 exactly
  `outcome unknown for this operation; not dispatched again`.
- **Batch ids:** a returned batch id must be in the ledger; an attempt that committed a
  batch must return that batch, and a replay must return a batch an earlier attempt committed.
- **Inputs:** checked on the `tools/call` arguments as sent on the wire, which must equal what
  the tool received. S02 attempts send identical semantic arguments; operation-id rows send
  the case's operation ids (S02: distinct, `op-1` then `op-2`).
- **Ledger:** every committed batch has exactly one notification per invited attendee, each
  carrying the semantic arguments; the batch and dispatch totals match.

`test_scheduling_fixture.py` runs the clean fixture and a set of deliberately broken ones
(`BREAKS` in the fixture: an error before the effect, an unguarded drop, a JSON-RPC error,
an unrelated or timeout-coded `MCPError` on the retry, a retry that redispatches, a reserved
record that returns success, a different post-commit error, a missing, duplicate or extra
ledger row, a ledger batch id the result does not name, a reused operation id, an operation
id that differs between the wire and the client's record, changed arguments, a recorded drop
followed by a forwarded timeout error, a malformed result). Each must fail the check that guards
it; the tests also confirm that several of them passed the earlier oracle (final batch count
plus fault presence).

## Run

From the repository root, with the root requirements:

```bash
pip install -r requirements.txt   # mcp==2.2.0
python experimental/retry-recovery/scheduling/scheduling_fixture.py --output results.json
python -m unittest discover -v -s experimental/retry-recovery/scheduling
```

The fixture takes about 10 s: S01 waits for the SDK read timeout (5 s, same as the root
scripts) twice. The tests use a 0.5 s read timeout and take about 4 s. No sleeps. The S03
fault is injected after the ledger write. `--output` writes per-attempt results, transport
faults, the ledger, source hashes and the installed distributions as JSON. The fixture exits
nonzero if any case fails.

## Archived run

[evidence/2026-11-05/](evidence/2026-11-05/): `scheduling-run.log` (stdout and stderr),
`scheduling-results.json`, `scheduling-unit-tests.log` (26 tests, OK) and `SHA256.json`.
Python 3.12.13, `mcp` 2.2.0, `mcp-types` 2.2.0, negotiated protocolVersion 2025-11-25.

stdout of the fixture, trace lines shortened (stderr also carries the SDK's INFO line
`Tool 'reschedule_meeting' failed: ...` for each error result):

```
trace {"row": "S01", "key": "argument", "store": "record-first", "attempt": 1, "jsonrpc_id": 2, "operation_id": null, "dispatches": 1, "effect_delta": 1, "status": "timeout", "batch": null}
trace {"row": "S01", "key": "argument", "store": "record-first", "attempt": 2, "jsonrpc_id": 3, "operation_id": null, "dispatches": 0, "effect_delta": 0, "status": "replayed", "batch": "batch-1"}
...
trace {"row": "S03", "key": "operation_id", "store": "record-first", "attempt": 2, "jsonrpc_id": 3, "operation_id": "op-1", "dispatches": 0, "effect_delta": 0, "status": "outcome_unknown", "batch": null}
negotiated protocolVersion: 2025-11-25
| Row | Key | Store | Intended | Expected | Observed | Dispatches | Effect deltas | Statuses | Batches | Label | Check |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S01 | argument | record-first | 1 | 1 | 1 | 1,0 | 1,0 | timeout,replayed | -,batch-1 | safe | pass |
| S01 | operation_id | record-first | 1 | 1 | 1 | 1,0 | 1,0 | timeout,replayed | -,batch-1 | safe | pass |
| S02 | argument | record-first | 2 | 1 | 1 | 1,0 | 1,0 | committed,replayed | batch-1,batch-1 | expected counterexample | pass |
| S02 | operation_id | record-first | 2 | 2 | 2 | 1,1 | 1,1 | committed,committed | batch-1,batch-2 | safe | pass |
| S03 | argument | success-only | 1 | 2 | 2 | 1,1 | 1,1 | injected_error,committed | -,batch-2 | expected counterexample | pass |
| S03 | operation_id | success-only | 1 | 2 | 2 | 1,1 | 1,1 | injected_error,committed | -,batch-2 | expected counterexample | pass |
| S03 | argument | record-first | 1 | 1 | 1 | 1,0 | 1,0 | injected_error,outcome_unknown | -,- | safe | pass |
| S03 | operation_id | record-first | 1 | 1 | 1 | 1,0 | 1,0 | injected_error,outcome_unknown | -,- | safe | pass |
8/8 cases match expectations
```

The trace has one line per attempt. JSON-RPC ids skip a number after a successful call,
because the client sends `tools/list` to validate the result. On S01 attempt 1 the client
gets no batch; the dropped result named `batch-1`, recorded in `transport_faults`.

## Limits

- In-process only: the dedup store and ledger are Python objects, not durable or crash-safe.
  Each case starts from empty state.
- Synthetic: no real notification provider. A local ledger result is not an exactly-once
  guarantee for a real provider; record-first only moves the gap to "outcome unknown".
- Not covered here: concurrency (C01), pruning or TTL (T01-T04), operation id reused with
  changed arguments (A06), and authorization.
