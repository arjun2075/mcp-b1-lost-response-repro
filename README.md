# B1 minimal reproduction — MCP `tools/call` lost-response retry

Companion reproduction for
[modelcontextprotocol/modelcontextprotocol#3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394):
on the tested MCP SDK path, a mutating `tools/call` commits its external
effect and then loses its response; the client perceives a timeout, retries
with a fresh JSON-RPC request ID, and the tool executes a second time —
**2 external effects for 1 logical operation**.

Built only on the real `mcp` 2.2.0 Python SDK (`ClientSession` →
`MCPServer` over memory streams, negotiated protocolVersion 2025-11-25).
No other project code is imported. The reproduction logic is identical to
the reviewed scripts; only the run instructions were adapted for
standalone use.

## Setup

Python 3.12, then:

```bash
pip install -r requirements.txt
```

## Run

```bash
python b1_lost_response_repro.py
python b1_positive_control.py
```

Each run takes ~7 seconds (a 5 s SDK read timeout on attempt 1, then one
retry). No sleeps are used for synchronization; the client-perceived
timeout comes from the SDK's `read_timeout_seconds`.

## Expected output (baseline)

```
negotiated protocolVersion: 2025-11-25
[fault] dropped attempt-1 tools/call response after commit (jsonrpc_id=2); client will perceive a timeout
attempt 1: client-perceived timeout (MCPError)
attempt 2: completed (effect_id=..., result=committed)
attempt-1 JSON-RPC id: 2
attempt-2 JSON-RPC id: 3
authoritative effect count: 2
BASELINE: 2 external effects for 1 logical operation (duplicate execution observed)
```

## Expected output (positive control)

```
negotiated protocolVersion: 2025-11-25
[fault] dropped attempt-1 tools/call response after commit (jsonrpc_id=2); client will perceive a timeout
attempt 1: client-perceived timeout (MCPError)
attempt 2: completed (effect_id=..., result=deduped)
attempt-1 JSON-RPC id: 2
attempt-2 JSON-RPC id: 3
authoritative effect count: 1
POSITIVE CONTROL: 1 external effect for 1 logical operation (retry absorbed by application-level dedup)
```

## Mechanism (5 lines)

1. Two relay tasks sit between the real client and server on the memory
   streams; the server→client relay deterministically drops the attempt-1
   `tools/call` response, which can only reach that relay after the server
   handler returned, so the tool's external effect is committed before the
   drop.
2. The client perceives a timeout via the SDK's `read_timeout_seconds`
   (raising `MCPError`); the SDK performs no automatic retry.
3. The reproduction performs exactly one application retry with identical
   arguments; the SDK allocates a fresh JSON-RPC id for it (printed for
   both attempts), so the server cannot correlate it with attempt 1.
4. An inline append-only ledger records one row per tool execution and is
   the authoritative effect count; baseline records 2 rows for 1 logical
   operation.
5. The positive control passes a stable `logical_operation_id` as a tool
   argument and dedups at the application effect layer, recording 1 row.

## Claim boundary

- The tested MCP SDK path permits a retrying application to execute a
  mutating tool more than once after an ambiguous lost-response outcome.
- `tools/call` defines no normative retry or duplicate-effect semantics
  for this case; the duplicate execution observed here is permitted by
  the tested path, not a protocol violation.
- Applications can mitigate using stable idempotency at the effect
  boundary. In the positive control, MCP did not dedup; the application
  effect layer did — one mitigation, not the only possible design.
