# Scheduling fixture: rows S01-S03

Contributed by @Majny for the retry-recovery matrix in [../README.md](../README.md).
Synthetic and in-process only. Based on a meeting-scheduling MCP server, sanitized: no
real data, prompts or provider calls.

## Tool

`reschedule_meeting(meeting_id, start, timezone, operation_id?)`. Its effect is one
notification per attendee (3 attendees), appended to a synthetic ledger. The ledger is the
effect oracle; the count reported below is the number of notification batches in it. The
dedup store is a separate dict and never touches the ledger.

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

| ID | Setup / fault | Key | Store | Intended | Observed | Label |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | Lost response after notifications committed; one retry, same arguments | argument | record-first | 1 | 1 | safe |
| S01 | same | operation id (same on retry) | record-first | 1 | 1 | safe |
| S02 | No fault; identical arguments sent again on purpose as a new operation | argument | record-first | 2 | 1 | expected counterexample |
| S02 | same | operation id (new id for the repeat) | record-first | 2 | 2 | safe |
| S03 | Notifications committed, then an error before the result; one retry | argument | success-only | 1 | 2 | expected counterexample |
| S03 | same | operation id | success-only | 1 | 2 | expected counterexample |
| S03 | same | argument | record-first | 1 | 1 | safe, retry gets "outcome unknown" |
| S03 | same | operation id | record-first | 1 | 1 | safe, retry gets "outcome unknown" |

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

## Run

From the repository root, with the root requirements:

```bash
pip install -r requirements.txt   # mcp==2.2.0
python experimental/retry-recovery/scheduling/scheduling_fixture.py
```

Takes about 10 s: S01 waits for the SDK read timeout (5 s, same as the root scripts) twice.
No sleeps. The S03 fault is injected after the ledger write. The script exits nonzero if
any observed count differs from the expected one, if the response is not dropped, or if
attempt 1 does not show the injected timeout or error.

Run locally on Python 3.12.13, `mcp` 2.2.0, negotiated protocolVersion 2025-11-25.

## Expected output

stdout (stderr also carries the SDK's INFO line `Tool 'reschedule_meeting' failed: ...`
for each error result):

```
trace {"row": "S01", "key": "argument", "store": "record-first", "attempt": 1, "jsonrpc_id": 2, "operation_id": null, "dispatches": 1, "effect_delta": 1, "client": "timeout (MCPError)"}
trace {"row": "S01", "key": "argument", "store": "record-first", "attempt": 2, "jsonrpc_id": 3, "operation_id": null, "dispatches": 0, "effect_delta": 0, "client": "ok: {\"batch\": \"batch-1\", \"notified\": 3, \"result\": \"replayed\"}"}
...
trace {"row": "S03", "key": "operation_id", "store": "record-first", "attempt": 2, "jsonrpc_id": 3, "operation_id": "op-1", "dispatches": 0, "effect_delta": 0, "client": "error: Error executing tool reschedule_meeting: outcome unknown for this operation; not dispatched again"}
negotiated protocolVersion: 2025-11-25
| Row | Key | Store | Intended | Expected | Observed | Label | Check |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S01 | argument | record-first | 1 | 1 | 1 | safe | pass |
| S01 | operation_id | record-first | 1 | 1 | 1 | safe | pass |
| S02 | argument | record-first | 2 | 1 | 1 | expected counterexample | pass |
| S02 | operation_id | record-first | 2 | 2 | 2 | safe | pass |
| S03 | argument | success-only | 1 | 2 | 2 | expected counterexample | pass |
| S03 | operation_id | success-only | 1 | 2 | 2 | expected counterexample | pass |
| S03 | argument | record-first | 1 | 1 | 1 | safe | pass |
| S03 | operation_id | record-first | 1 | 1 | 1 | safe | pass |
8/8 cases match expectations
```

The trace has one line per attempt: JSON-RPC id, operation id, dispatches and effect delta
for that attempt, and what the client saw. JSON-RPC ids skip a number after a successful
call, because the client sends `tools/list` to validate the result.

## Limits

- In-process only: the dedup store and ledger are Python objects, not durable or crash-safe.
  Each case starts from empty state.
- Synthetic: no real notification provider. A local ledger result is not an exactly-once
  guarantee for a real provider; record-first only moves the gap to "outcome unknown".
- Not covered here: concurrency (C01), pruning or TTL (T01-T04), operation id reused with
  changed arguments (A06), and authorization.
