"""Scheduling fixture for the retry-recovery matrix (rows S01-S03).

A synthetic meeting-scheduling MCP server exposes one mutating tool,
reschedule_meeting(meeting_id, start, timezone, operation_id?). Its effect
is one notification per attendee, appended to a synthetic ledger (the
effect oracle). The dedup store is separate from the ledger. Both are
in-process only, not durable and not crash-safe.

Each case runs on the real mcp 2.2.0 client and server over in-memory
streams, with the same response-drop relay as the root scripts. It compares
two ways of deriving the dedup key:
  - argument key: digest of principal, tool and semantic arguments
  - operation id: caller-supplied operation_id, scoped to principal and tool
and two store policies:
  - success-only: cache a result only after the tool succeeds
  - record-first: record the operation before the effect, complete it after

Rows marked "expected counterexample" demonstrate unsafe behavior on
purpose (like R04). Every attempt is checked against an expected status,
dispatch count, effect delta and batch, and the ledger is checked row by
row. The script exits nonzero if any check fails.

Run:
    pip install -r requirements.txt   # mcp==2.2.0, Python 3.12
    python experimental/retry-recovery/scheduling/scheduling_fixture.py \
        [--output results.json]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anyio

from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams
from mcp_types.jsonrpc import (
    INTERNAL_ERROR,
    REQUEST_TIMEOUT,
    ErrorData,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
)

READ_TIMEOUT_SECS = 5.0
SERVER_SHUTDOWN_GRACE_SECS = 2.0
PRINCIPAL = "principal-fixture-1"  # stands in for the authenticated caller
ATTENDEES = ("attendee-1", "attendee-2", "attendee-3")
ARGS = {
    "meeting_id": "meeting-fixture-1",
    "start": "2026-11-03T10:00",
    "timezone": "Europe/Prague",
}
TOOL = "reschedule_meeting"
INJECTED_ERROR = "injected fault: notifications committed, then an error before the result"
OUTCOME_UNKNOWN = "outcome unknown for this operation; not dispatched again"
# MCPServer prefixes a ToolError message with this in the error result.
TOOL_ERROR_PREFIX = f"Error executing tool {TOOL}: "
DROPPED = "response_dropped_after_commit"
# The SDK's own read-timeout MCPError text for a tools/call (mcp 2.2.0).
SDK_TIMEOUT = "Request 'tools/call' timed out"

# Deliberate breakages, used only by test_scheduling_fixture.py to show that
# each oracle check fails when the behavior it guards is broken. A normal run
# uses none.
BREAKS = frozenset({
    "error_before_effect",  # attempt 1 fails before any effect
    "drop_any_first_response",  # relay drops the first response without its guards
    "no_drop",  # relay never drops
    "jsonrpc_error_first",  # first tools/call response replaced by a JSON-RPC error
    "unrelated_mcp_error_on_retry",  # second response replaced by an unrelated error
    "timeout_code_on_retry",  # second response replaced by a timeout-coded error
    "retry_redispatches",  # dedup lookup skipped after the first call
    "unknown_returns_success",  # reserved record replays a success
    "wrong_injected_error",  # post-commit error with a different message
    "ledger_missing_row",  # one attendee row not written
    "ledger_duplicate_row",  # one attendee row written twice
    "ledger_extra_attendee",  # a row for an attendee who is not invited
    "same_operation_id_on_repeat",  # S02 repeat reuses the first operation id
    "changed_args_on_repeat",  # S02 repeat changes the semantic arguments
    "wire_operation_id_differs",  # S02 repeat sends op-1, the client still records op-2
    "drop_recorded_but_error_forwarded",  # relay records the drop, forwards a timeout error
    "ledger_batch_id_differs",  # ledger rows under a batch id the result does not name
    "non_mcp_exception_on_retry",  # second response is a malformed tools/call result
})


@dataclass(frozen=True)
class Attempt:
    # "timeout" | "committed" | "replayed" | "injected_error" | "outcome_unknown"
    status: str
    dispatches: int  # tool dispatches during this attempt
    effect_delta: int  # notification batches added to the ledger during this attempt
    batch: str | None  # batch id the client was given, None if no result


@dataclass(frozen=True)
class Case:
    row: str
    key: str  # "argument" | "operation_id"
    store: str  # "success-only" | "record-first"
    fault: str  # "lost" | "error" | "none"
    operation_ids: tuple[str, ...]  # per attempt; ignored for the argument key
    intended: int  # notification batches the caller meant to cause
    attempts: tuple[Attempt, ...]  # expected per-attempt outcome

    @property
    def expected(self) -> int:
        return sum(a.effect_delta for a in self.attempts)

    @property
    def label(self) -> str:
        return "expected counterexample" if self.expected != self.intended else "safe"


LOST_REPLAYED = (Attempt("timeout", 1, 1, None), Attempt("replayed", 0, 0, "batch-1"))
ERROR_REDISPATCHED = (Attempt("injected_error", 1, 1, None), Attempt("committed", 1, 1, "batch-2"))
ERROR_UNKNOWN = (Attempt("injected_error", 1, 1, None), Attempt("outcome_unknown", 0, 0, None))

# Fault: "lost" = response dropped after the effect, "error" = effect committed,
# then an error before the result, "none" = no fault (deliberate repeat).
CASES = [
    Case("S01", "argument", "record-first", "lost", ("op-1", "op-1"), 1, LOST_REPLAYED),
    Case("S01", "operation_id", "record-first", "lost", ("op-1", "op-1"), 1, LOST_REPLAYED),
    Case("S02", "argument", "record-first", "none", ("op-1", "op-2"), 2,
         (Attempt("committed", 1, 1, "batch-1"), Attempt("replayed", 0, 0, "batch-1"))),
    Case("S02", "operation_id", "record-first", "none", ("op-1", "op-2"), 2,
         (Attempt("committed", 1, 1, "batch-1"), Attempt("committed", 1, 1, "batch-2"))),
    Case("S03", "argument", "success-only", "error", ("op-1", "op-1"), 1, ERROR_REDISPATCHED),
    Case("S03", "operation_id", "success-only", "error", ("op-1", "op-1"), 1, ERROR_REDISPATCHED),
    Case("S03", "argument", "record-first", "error", ("op-1", "op-1"), 1, ERROR_UNKNOWN),
    Case("S03", "operation_id", "record-first", "error", ("op-1", "op-1"), 1, ERROR_UNKNOWN),
]


@dataclass
class ServerState:
    # Effect oracle: append-only, separate from the dedup store.
    ledger: list[dict[str, Any]] = field(default_factory=list)
    # Dedup store: in-process only, not durable.
    dedup: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Arguments as the tool received them, one entry per call.
    received: list[dict[str, Any]] = field(default_factory=list)
    dispatches: int = 0
    calls: int = 0

    def batches(self) -> list[str]:
        """Batch ids in the ledger, in commit order."""
        return list(dict.fromkeys(row["batch"] for row in self.ledger))


def ledger_problems(ledger: list[dict[str, Any]], args: dict[str, Any] = ARGS) -> list[str]:
    """Every committed batch must hold exactly one row per expected attendee,
    each carrying the semantic arguments; anything else is reported."""
    problems = []
    batches: dict[str, list[dict[str, Any]]] = {}
    for row in ledger:
        batches.setdefault(row["batch"], []).append(row)
    for batch, rows in batches.items():
        attendees = sorted(row["attendee"] for row in rows)
        if attendees != sorted(ATTENDEES):
            problems.append(f"ledger {batch}: attendees {attendees}, "
                            f"expected one each of {list(ATTENDEES)}")
        for row in rows:
            semantic = {k: row.get(k) for k in args}
            if semantic != args:
                problems.append(f"ledger {batch}: row {row['attendee']} carries {semantic}")
    return problems


def dedup_key(case: Case, args: dict[str, Any], operation_id: str | None) -> str:
    if case.key == "operation_id":
        if not operation_id:
            raise ToolError("operation_id is required in this configuration")
        return f"{PRINCIPAL}/{TOOL}/op:{operation_id}"
    digest = hashlib.sha256(json.dumps([PRINCIPAL, TOOL, args], sort_keys=True).encode())
    return f"{PRINCIPAL}/{TOOL}/args:{digest.hexdigest()[:12]}"


def build_server(case: Case, state: ServerState, breaks: frozenset[str] = frozenset()) -> MCPServer:
    srv = MCPServer(name="scheduling-fixture")

    @srv.tool()
    def reschedule_meeting(
        meeting_id: str, start: str, timezone: str, operation_id: str | None = None
    ) -> str:
        state.calls += 1
        state.received.append({"meeting_id": meeting_id, "start": start,
                               "timezone": timezone, "operation_id": operation_id})
        if "error_before_effect" in breaks and state.calls == 1:
            raise ToolError("broken fixture: failed before any effect")
        args = {"meeting_id": meeting_id, "start": start, "timezone": timezone}
        key = dedup_key(case, args, operation_id)
        entry = state.dedup.get(key)
        if "retry_redispatches" in breaks and state.calls > 1:
            entry = None
        if entry is not None:
            if entry["state"] == "completed":
                return json.dumps({**entry["result"], "result": "replayed"})
            if "unknown_returns_success" in breaks:
                return json.dumps({"batch": "batch-1", "notified": len(ATTENDEES),
                                   "result": "replayed"})
            # Recorded but never completed: the effect may or may not have happened.
            raise ToolError(OUTCOME_UNKNOWN)
        if case.store == "record-first":
            state.dedup[key] = {"state": "reserved"}
        state.dispatches += 1
        batch = f"batch-{state.dispatches}"
        attendees = list(ATTENDEES)
        if "ledger_missing_row" in breaks:
            attendees = attendees[:-1]
        if "ledger_duplicate_row" in breaks:
            attendees.append(attendees[0])
        if "ledger_extra_attendee" in breaks:
            attendees.append("attendee-uninvited")
        ledger_batch = "n" + batch if "ledger_batch_id_differs" in breaks else batch
        for attendee in attendees:
            state.ledger.append({"batch": ledger_batch, "attendee": attendee, **args})
        if case.fault == "error" and state.dispatches == 1:
            if "wrong_injected_error" in breaks:
                raise ToolError("provider rejected the request")
            raise ToolError(INJECTED_ERROR)
        result = {"batch": batch, "notified": len(ATTENDEES)}
        state.dedup[key] = {"state": "completed", "result": result}
        return json.dumps({**result, "result": "committed"})

    return srv


def wire_result(rpc: JSONRPCResponse) -> tuple[bool, dict[str, Any] | None]:
    """(isError, parsed JSON text) of a tools/call result as sent on the wire."""
    result = rpc.result if isinstance(rpc.result, dict) else {}
    texts = [b.get("text") for b in result.get("content", []) if b.get("type") == "text"]
    try:
        payload = json.loads(texts[0]) if len(texts) == 1 else None
    except ValueError:
        payload = None
    return bool(result.get("isError")), payload if isinstance(payload, dict) else None


def classify(res: Any) -> tuple[str, dict[str, Any] | None, str]:
    """Client-visible status, parsed payload and raw text of a CallToolResult."""
    texts = [b.text for b in res.content if b.type == "text"]
    text = texts[0] if len(texts) == 1 else ""
    if res.is_error:
        if text == TOOL_ERROR_PREFIX + INJECTED_ERROR:
            return "injected_error", None, text
        if text == TOOL_ERROR_PREFIX + OUTCOME_UNKNOWN:
            return "outcome_unknown", None, text
        return "unexpected_error", None, text
    try:
        payload = json.loads(text)
    except ValueError:
        return "unexpected_result", None, text
    if (
        isinstance(payload, dict)
        and payload.get("result") in ("committed", "replayed")
        and payload.get("notified") == len(ATTENDEES)
    ):
        return payload["result"], payload, text
    return "unexpected_result", payload if isinstance(payload, dict) else None, text


async def run_case(
    case: Case,
    breaks: frozenset[str] = frozenset(),
    read_timeout: float = READ_TIMEOUT_SECS,
) -> dict[str, Any]:
    unknown = set(breaks) - BREAKS
    if unknown:
        raise ValueError(f"unknown breaks {sorted(unknown)}")
    state = ServerState()
    server = build_server(case, state, breaks)
    problems: list[str] = []
    streams_cm = create_client_server_memory_streams()
    client_streams, server_streams = await streams_cm.__aenter__()
    c_read, c_write = client_streams
    s_read, s_write = server_streams
    c2r_send, c2r_recv = anyio.create_memory_object_stream(256)
    r2c_send, r2c_recv = anyio.create_memory_object_stream(256)
    tools_call_ids: list[Any] = []
    tools_call_args: list[dict[str, Any]] = []  # arguments as sent on the wire
    forwarded_ids: list[Any] = []  # ids of replies the relay forwarded to the client
    faults: list[dict[str, Any]] = []
    server_done = anyio.Event()

    async def relay_c2s() -> None:
        async with c2r_recv, c_write:
            async for msg in c2r_recv:
                if isinstance(msg, SessionMessage) and isinstance(
                    msg.message, JSONRPCRequest
                ):
                    if msg.message.method == "tools/call":
                        tools_call_ids.append(msg.message.id)
                        params = msg.message.params or {}
                        tools_call_args.append(dict(params.get("arguments") or {}))
                await c_write.send(msg)

    def replace_with_error(msg: SessionMessage, code: int, message: str) -> SessionMessage:
        faults.append({"event": "response_replaced", "jsonrpc_id": msg.message.id,
                       "code": code, "message": message})
        return SessionMessage(JSONRPCError(
            jsonrpc="2.0", id=msg.message.id, error=ErrorData(code=code, message=message)))

    def drop_refusal(rpc: Any) -> str | None:
        """Why the first response must not be dropped, or None if it may be.

        Only a successful tools/call result for an effect that is already
        committed may be dropped: one dispatch, one complete batch, and the
        result names that batch.
        """
        if not isinstance(rpc, JSONRPCResponse):
            return f"first response is a {type(rpc).__name__}, not a JSONRPCResponse"
        is_error, payload = wire_result(rpc)
        if is_error:
            return "first response is a tool error result, not a success"
        if state.dispatches != 1 or len(state.batches()) != 1:
            return f"{state.dispatches} dispatches, batches {state.batches()} before the drop"
        if ledger_problems(state.ledger):
            return "ledger is not one complete batch before the drop"
        if not payload or payload.get("batch") != state.batches()[0]:
            return f"result {payload!r} does not name the committed batch"
        return None

    async def relay_s2c() -> None:
        # Same fault hook as the root scripts: the attempt-1 response only
        # exists once the handler returned, so the drop is after the effect.
        async with c_read, r2c_send:
            async for msg in c_read:
                rpc = msg.message if isinstance(msg, SessionMessage) else None
                reply = isinstance(rpc, (JSONRPCResponse, JSONRPCError))
                first = reply and bool(tools_call_ids) and rpc.id == tools_call_ids[0]
                second = reply and len(tools_call_ids) > 1 and rpc.id == tools_call_ids[1]
                if first and "jsonrpc_error_first" in breaks:
                    msg = replace_with_error(msg, INTERNAL_ERROR, "broken fixture: error response")
                    rpc = msg.message
                if second and "unrelated_mcp_error_on_retry" in breaks:
                    msg = replace_with_error(msg, INTERNAL_ERROR, "broken fixture: unrelated failure")
                if second and "timeout_code_on_retry" in breaks:
                    msg = replace_with_error(msg, REQUEST_TIMEOUT, SDK_TIMEOUT)
                if second and "non_mcp_exception_on_retry" in breaks:
                    msg = SessionMessage(JSONRPCResponse(
                        jsonrpc="2.0", id=rpc.id, result={"content": "not a list"}))
                if (
                    first
                    and case.fault == "lost"
                    and "no_drop" not in breaks
                    and not any(f["event"] == DROPPED for f in faults)
                ):
                    refusal = None if "drop_any_first_response" in breaks else drop_refusal(rpc)
                    if refusal is None:
                        payload = wire_result(rpc)[1] if isinstance(rpc, JSONRPCResponse) else None
                        faults.append({"event": DROPPED, "jsonrpc_id": rpc.id, "payload": payload})
                        if "drop_recorded_but_error_forwarded" not in breaks:
                            continue
                        msg = SessionMessage(JSONRPCError(jsonrpc="2.0", id=rpc.id, error=ErrorData(
                            code=REQUEST_TIMEOUT, message=SDK_TIMEOUT)))
                    else:
                        problems.append(f"drop refused: {refusal}")
                if reply:
                    forwarded_ids.append(msg.message.id)
                await r2c_send.send(msg)

    async def run_server() -> None:
        lowlevel: Server = server._lowlevel_server
        try:
            await lowlevel.run(
                s_read,
                s_write,
                lowlevel.create_initialization_options(),
                raise_exceptions=False,
            )
        finally:
            server_done.set()

    attempts: list[dict[str, Any]] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_server)
        tg.start_soon(relay_c2s)
        tg.start_soon(relay_s2c)
        session = ClientSession(r2c_recv, c2r_send)
        await session.__aenter__()
        init = await session.initialize()

        for attempt, operation_id in enumerate(case.operation_ids, start=1):
            args = dict(ARGS)
            if attempt == 2 and "changed_args_on_repeat" in breaks:
                args["start"] = "2026-11-03T11:00"
            if attempt == 2 and "same_operation_id_on_repeat" in breaks:
                operation_id = case.operation_ids[0]
            if case.key == "operation_id":
                args["operation_id"] = operation_id
                if attempt == 2 and "wire_operation_id_differs" in breaks:
                    args["operation_id"] = case.operation_ids[0]
            before_dispatches, before_batches = state.dispatches, len(state.batches())
            calls_before = len(tools_call_ids)
            payload, text, exception = None, None, None
            try:
                res = await session.call_tool(TOOL, args, read_timeout_seconds=read_timeout)
            except MCPError as exc:
                exception = {"type": type(exc).__name__, "code": exc.code, "message": exc.message}
                jsonrpc_id = tools_call_ids[-1] if len(tools_call_ids) > calls_before else None
                dropped = [f for f in faults if f["event"] == DROPPED]
                # Only the deliberately dropped attempt-1 response counts as a timeout:
                # the SDK's own timeout, and no reply for that id reached the client.
                if (
                    case.fault == "lost"
                    and attempt == 1
                    and exc.code == REQUEST_TIMEOUT
                    and exc.message == SDK_TIMEOUT
                    and len(dropped) == 1
                    and dropped[0]["jsonrpc_id"] == jsonrpc_id
                    and jsonrpc_id not in forwarded_ids
                ):
                    status = "timeout"
                else:
                    status = "unexpected_exception"
                    problems.append(f"attempt {attempt}: unexpected {type(exc).__name__} "
                                    f"code {exc.code}: {exc.message}")
            except Exception as exc:  # noqa: BLE001 - any other exception fails the case
                exception = {"type": type(exc).__name__, "code": None, "message": str(exc)}
                status = "unexpected_exception"
                problems.append(f"attempt {attempt}: unexpected {type(exc).__name__}: {exc}")
            else:
                status, payload, text = classify(res)
            # Arguments as sent on the wire, not the client's local copy.
            sent = tools_call_args[calls_before] if len(tools_call_args) > calls_before else {}
            attempts.append({
                "attempt": attempt,
                "jsonrpc_id": tools_call_ids[-1] if len(tools_call_ids) > calls_before else None,
                "operation_id": sent.get("operation_id"),
                "arguments": {k: sent.get(k) for k in ARGS},
                "dispatches": state.dispatches - before_dispatches,
                "effect_delta": len(state.batches()) - before_batches,
                "committed_batches": state.batches()[before_batches:],
                "status": status,
                "batch": payload.get("batch") if payload else None,
                "payload": payload,
                "client_text": text,
                "exception": exception,
            })

        await session.__aexit__(None, None, None)
        await c2r_send.aclose()
        with anyio.move_on_after(SERVER_SHUTDOWN_GRACE_SECS):
            await server_done.wait()
        tg.cancel_scope.cancel()

    await streams_cm.__aexit__(None, None, None)
    problems += oracle_problems(case, attempts, faults, tools_call_ids, state)
    return {
        "row": case.row, "key": case.key, "store": case.store, "fault": case.fault,
        "label": case.label, "intended": case.intended, "expected": case.expected,
        "observed": len(state.batches()), "passed": not problems, "problems": problems,
        "protocol_version": init.protocol_version, "jsonrpc_ids": tools_call_ids,
        "forwarded_reply_ids": forwarded_ids, "server_received": state.received,
        "transport_faults": faults, "expected_attempts": [asdict(a) for a in case.attempts],
        "attempts": attempts, "ledger": state.ledger,
    }


def oracle_problems(
    case: Case,
    attempts: list[dict[str, Any]],
    faults: list[dict[str, Any]],
    tools_call_ids: list[Any],
    state: ServerState,
) -> list[str]:
    problems = []
    n = len(case.attempts)
    if len(tools_call_ids) != n or len(set(tools_call_ids)) != n:
        problems.append(f"unexpected tools/call ids {tools_call_ids!r}")
    if len(attempts) != n:
        return problems + [f"{len(attempts)} attempts, expected {n}"]

    # Per attempt: status, dispatches, effect delta and batch.
    for seen, want in zip(attempts, case.attempts):
        for name in ("status", "dispatches", "effect_delta", "batch"):
            if seen[name] != getattr(want, name):
                problems.append(f"attempt {seen['attempt']}: {name} {seen[name]!r}, "
                                f"expected {getattr(want, name)!r}")

    # Lost response: exactly one drop, of attempt 1's response, and a replay
    # returns exactly the dropped result.
    dropped = [f for f in faults if f["event"] == DROPPED]
    if case.fault == "lost":
        if len(dropped) != 1 or dropped[0]["jsonrpc_id"] != attempts[0]["jsonrpc_id"]:
            problems.append(f"expected one drop of attempt 1's response, got {dropped!r}")
        elif attempts[1]["status"] == "replayed":
            original = dict(dropped[0]["payload"] or {}, result="replayed")
            if attempts[1]["payload"] != original:
                problems.append(f"attempt 2 replayed {attempts[1]['payload']!r}, "
                                f"not the dropped result {dropped[0]['payload']!r}")
    elif dropped:
        problems.append(f"unexpected drop {dropped!r}")

    # Without a lost response, a replay equals attempt 1's delivered result.
    if case.fault != "lost":
        for seen in attempts[1:]:
            if seen["status"] == "replayed":
                original = dict(attempts[0]["payload"] or {}, result="replayed")
                if seen["payload"] != original:
                    problems.append(f"attempt {seen['attempt']} replayed {seen['payload']!r}, "
                                    f"not attempt 1's {attempts[0]['payload']!r}")

    # Batch ids: a returned batch must be in the ledger. An attempt that
    # committed a batch returns that batch; a replay returns one an earlier
    # attempt committed.
    ledger_batches = state.batches()
    for i, seen in enumerate(attempts):
        if seen["batch"] is None:
            continue
        if seen["batch"] not in ledger_batches:
            problems.append(f"attempt {seen['attempt']}: batch {seen['batch']!r} "
                            f"is not in the ledger {ledger_batches}")
        if seen["effect_delta"] and seen["committed_batches"] != [seen["batch"]]:
            problems.append(f"attempt {seen['attempt']}: returned {seen['batch']!r}, "
                            f"committed {seen['committed_batches']}")
        earlier = [b for a in attempts[:i] for b in a["committed_batches"]]
        if seen["status"] == "replayed" and seen["batch"] not in earlier:
            problems.append(f"attempt {seen['attempt']}: replayed {seen['batch']!r}, "
                            f"not a batch an earlier attempt committed {earlier}")

    # Inputs: the wire arguments are the ones the tool received, and for the
    # operation-id key each attempt sent the case's operation id.
    sent = [dict(a["arguments"], operation_id=a["operation_id"]) for a in attempts]
    if sent != state.received:
        problems.append(f"tool received {state.received}, wire sent {sent}")
    if case.key == "operation_id":
        ops = [a["operation_id"] for a in attempts]
        if ops != list(case.operation_ids):
            problems.append(f"sent operation ids {ops}, expected {list(case.operation_ids)}")

    # Deliberate repeat: identical semantic arguments; the positive control
    # sends distinct caller operation ids (both as sent on the wire).
    if case.fault == "none":
        if len({json.dumps(a["arguments"], sort_keys=True) for a in attempts}) != 1:
            problems.append("repeat changed the semantic arguments: "
                            f"{[a['arguments'] for a in attempts]}")
        if case.key == "operation_id":
            ops = [a["operation_id"] for a in attempts]
            if len(set(ops)) != len(ops):
                problems.append(f"positive control reused an operation id: {ops}")

    # Effect oracle: total batches and dispatches, and every row of every batch.
    if len(state.batches()) != case.expected:
        problems.append(f"ledger has {len(state.batches())} batches, expected {case.expected}")
    if state.dispatches != sum(a.dispatches for a in case.attempts):
        problems.append(f"{state.dispatches} dispatches in total")
    problems += ledger_problems(state.ledger)
    return problems


def fmt(values: list[Any]) -> str:
    return ",".join("-" if v is None else str(v) for v in values)


def table(results: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Row | Key | Store | Intended | Expected | Observed | Dispatches | Effect deltas "
        "| Statuses | Batches | Label | Check |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        att = r["attempts"]
        check = "pass" if r["passed"] else "FAIL " + "; ".join(r["problems"])
        lines.append(
            f"| {r['row']} | {r['key']} | {r['store']} | {r['intended']} | {r['expected']} "
            f"| {r['observed']} | {fmt([a['dispatches'] for a in att])} "
            f"| {fmt([a['effect_delta'] for a in att])} | {fmt([a['status'] for a in att])} "
            f"| {fmt([a['batch'] for a in att])} | {r['label']} | {check} |"
        )
    return lines


async def run_all() -> list[dict[str, Any]]:
    results = []
    for case in CASES:
        result = await run_case(case)
        for a in result["attempts"]:
            trace = {k: a[k] for k in ("attempt", "jsonrpc_id", "operation_id", "dispatches",
                                       "effect_delta", "status", "batch")}
            print("trace " + json.dumps({"row": case.row, "key": case.key,
                                         "store": case.store, **trace}))
        results.append(result)
    return results


def environment_freeze() -> dict[str, Any]:
    """Installed distributions as name==version (the venv may have no pip)."""
    lines = sorted({f"{d.metadata['Name']}=={d.version}"
                    for d in importlib.metadata.distributions()}, key=str.lower)
    return {"distributions": lines,
            "distributions_sha256": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="write structured results as JSON")
    args = parser.parse_args()
    results = anyio.run(run_all)
    versions = sorted({r["protocol_version"] for r in results})
    print(f"negotiated protocolVersion: {', '.join(versions)}")
    print("\n".join(table(results)))
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} cases match expectations")
    if args.output:
        here = Path(__file__).resolve()
        report = {
            "python": platform.python_version(),
            "mcp": importlib.metadata.version("mcp"),
            "mcp_types": importlib.metadata.version("mcp-types"),
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                              (here, here.with_name("test_scheduling_fixture.py"))},
            "environment": environment_freeze(),
            "read_timeout_s": READ_TIMEOUT_SECS,
            "results": results, "passed": passed, "total": len(results),
            "scope": "real MCP memory streams; synthetic in-process ledger and dedup store",
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8",
                               newline="\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
