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
purpose (like R04). The script exits nonzero if any observed effect count
differs from the expected one, or if an injected fault did not happen.

Run:
    pip install -r requirements.txt   # mcp==2.2.0, Python 3.12
    python experimental/retry-recovery/scheduling/scheduling_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import anyio

from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams
from mcp_types.jsonrpc import JSONRPCError, JSONRPCRequest, JSONRPCResponse

READ_TIMEOUT_SECS = 5.0
SERVER_SHUTDOWN_GRACE_SECS = 2.0
PRINCIPAL = "principal-fixture-1"  # stands in for the authenticated caller
ATTENDEES = ("attendee-1", "attendee-2", "attendee-3")
PROTOCOL_VERSIONS: set[str] = set()
ARGS = {
    "meeting_id": "meeting-fixture-1",
    "start": "2026-11-03T10:00",
    "timezone": "Europe/Prague",
}


@dataclass(frozen=True)
class Case:
    row: str
    key: str  # "argument" | "operation_id"
    store: str  # "success-only" | "record-first"
    fault: str  # "lost" | "error" | "none"
    operation_ids: tuple[str, ...]  # per attempt; ignored for the argument key
    intended: int  # notification batches the caller meant to cause
    expected: int  # batches this configuration is expected to produce

    @property
    def label(self) -> str:
        return "expected counterexample" if self.expected != self.intended else "safe"


# Fault: "lost" = response dropped after the effect, "error" = effect committed,
# then an error before the result, "none" = no fault (deliberate repeat).
CASES = [
    Case("S01", "argument", "record-first", "lost", ("op-1", "op-1"), 1, 1),
    Case("S01", "operation_id", "record-first", "lost", ("op-1", "op-1"), 1, 1),
    Case("S02", "argument", "record-first", "none", ("op-1", "op-2"), 2, 1),
    Case("S02", "operation_id", "record-first", "none", ("op-1", "op-2"), 2, 2),
    Case("S03", "argument", "success-only", "error", ("op-1", "op-1"), 1, 2),
    Case("S03", "operation_id", "success-only", "error", ("op-1", "op-1"), 1, 2),
    Case("S03", "argument", "record-first", "error", ("op-1", "op-1"), 1, 1),
    Case("S03", "operation_id", "record-first", "error", ("op-1", "op-1"), 1, 1),
]


@dataclass
class ServerState:
    # Effect oracle: append-only, separate from the dedup store.
    ledger: list[dict[str, Any]] = field(default_factory=list)
    # Dedup store: in-process only, not durable.
    dedup: dict[str, dict[str, Any]] = field(default_factory=dict)
    dispatches: int = 0

    def batches(self) -> int:
        return len({row["batch"] for row in self.ledger})


def dedup_key(case: Case, args: dict[str, Any], operation_id: str | None) -> str:
    if case.key == "operation_id":
        if not operation_id:
            raise ToolError("operation_id is required in this configuration")
        return f"{PRINCIPAL}/reschedule_meeting/op:{operation_id}"
    digest = hashlib.sha256(
        json.dumps([PRINCIPAL, "reschedule_meeting", args], sort_keys=True).encode()
    )
    return f"{PRINCIPAL}/reschedule_meeting/args:{digest.hexdigest()[:12]}"


def build_server(case: Case, state: ServerState) -> MCPServer:
    srv = MCPServer(name="scheduling-fixture")

    @srv.tool()
    def reschedule_meeting(
        meeting_id: str, start: str, timezone: str, operation_id: str | None = None
    ) -> str:
        args = {"meeting_id": meeting_id, "start": start, "timezone": timezone}
        key = dedup_key(case, args, operation_id)
        entry = state.dedup.get(key)
        if entry is not None:
            if entry["state"] == "completed":
                return json.dumps({**entry["result"], "result": "replayed"})
            # Recorded but never completed: the effect may or may not have happened.
            raise ToolError("outcome unknown for this operation; not dispatched again")
        if case.store == "record-first":
            state.dedup[key] = {"state": "reserved"}
        state.dispatches += 1
        batch = f"batch-{state.dispatches}"
        for attendee in ATTENDEES:
            state.ledger.append({"batch": batch, "attendee": attendee, **args})
        if case.fault == "error" and state.dispatches == 1:
            raise ToolError(
                "injected fault: notifications committed, then an error before the result"
            )
        result = {"batch": batch, "notified": len(ATTENDEES)}
        state.dedup[key] = {"state": "completed", "result": result}
        return json.dumps({**result, "result": "committed"})

    return srv


def client_view(res: Any) -> str:
    text = next((b.text for b in res.content if b.type == "text"), "")
    return f"error: {text}" if res.is_error else f"ok: {text}"


async def run_case(case: Case) -> tuple[int, list[str]]:
    state = ServerState()
    server = build_server(case, state)
    problems: list[str] = []
    streams_cm = create_client_server_memory_streams()
    client_streams, server_streams = await streams_cm.__aenter__()
    c_read, c_write = client_streams
    s_read, s_write = server_streams
    c2r_send, c2r_recv = anyio.create_memory_object_stream(256)
    r2c_send, r2c_recv = anyio.create_memory_object_stream(256)
    tools_call_ids: list[Any] = []
    dropped: list[Any] = []
    server_done = anyio.Event()

    async def relay_c2s() -> None:
        async with c2r_recv, c_write:
            async for msg in c2r_recv:
                if isinstance(msg, SessionMessage) and isinstance(
                    msg.message, JSONRPCRequest
                ):
                    if msg.message.method == "tools/call":
                        tools_call_ids.append(msg.message.id)
                await c_write.send(msg)

    async def relay_s2c() -> None:
        # Same fault hook as the root scripts: the attempt-1 response only
        # exists once the handler returned, so the drop is after the effect.
        async with c_read, r2c_send:
            async for msg in c_read:
                if (
                    case.fault == "lost"
                    and not dropped
                    and isinstance(msg, SessionMessage)
                    and isinstance(msg.message, (JSONRPCResponse, JSONRPCError))
                    and tools_call_ids
                    and msg.message.id == tools_call_ids[0]
                ):
                    dropped.append(msg.message.id)
                    continue
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

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_server)
        tg.start_soon(relay_c2s)
        tg.start_soon(relay_s2c)
        session = ClientSession(r2c_recv, c2r_send)
        await session.__aenter__()
        init = await session.initialize()
        PROTOCOL_VERSIONS.add(init.protocol_version)

        for attempt, operation_id in enumerate(case.operation_ids, start=1):
            args = dict(ARGS)
            if case.key == "operation_id":
                args["operation_id"] = operation_id
            before_dispatches, before_batches = state.dispatches, state.batches()
            try:
                res = await session.call_tool(
                    "reschedule_meeting", args, read_timeout_seconds=READ_TIMEOUT_SECS
                )
                seen = client_view(res)
            except MCPError as exc:
                seen = f"timeout ({type(exc).__name__})"
            trace = {
                "row": case.row,
                "key": case.key,
                "store": case.store,
                "attempt": attempt,
                "jsonrpc_id": tools_call_ids[-1],
                "operation_id": operation_id if case.key == "operation_id" else None,
                "dispatches": state.dispatches - before_dispatches,
                "effect_delta": state.batches() - before_batches,
                "client": seen,
            }
            print("trace " + json.dumps(trace))
            if attempt == 1 and case.fault == "lost" and not seen.startswith("timeout"):
                problems.append("attempt 1 did not time out")
            if attempt == 1 and case.fault == "error" and not seen.startswith("error"):
                problems.append("attempt 1 did not return the injected error")

        await session.__aexit__(None, None, None)
        await c2r_send.aclose()
        with anyio.move_on_after(SERVER_SHUTDOWN_GRACE_SECS):
            await server_done.wait()
        tg.cancel_scope.cancel()

    await streams_cm.__aexit__(None, None, None)
    if case.fault == "lost" and not dropped:
        problems.append("attempt-1 response was not dropped")
    attempts = len(case.operation_ids)
    if len(tools_call_ids) != attempts or len(set(tools_call_ids)) != attempts:
        problems.append(f"unexpected tools/call ids {tools_call_ids!r}")
    return state.batches(), problems


async def main() -> int:
    rows = []
    for case in CASES:
        observed, problems = await run_case(case)
        ok = observed == case.expected and not problems
        rows.append((case, observed, ok, problems))

    print(f"negotiated protocolVersion: {', '.join(sorted(PROTOCOL_VERSIONS))}")
    print("| Row | Key | Store | Intended | Expected | Observed | Label | Check |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for case, observed, ok, problems in rows:
        check = (
            "pass"
            if ok
            else "FAIL " + "; ".join(problems or [f"expected {case.expected}"])
        )
        print(
            f"| {case.row} | {case.key} | {case.store} | {case.intended} | {case.expected} "
            f"| {observed} | {case.label} | {check} |"
        )
    failed = [r for r in rows if not r[2]]
    print(f"{len(rows) - len(failed)}/{len(rows)} cases match expectations")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
