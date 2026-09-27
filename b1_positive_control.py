"""Positive control for the B1 minimal reproduction.

Same failure as b1_lost_response_repro.py (attempt-1 tools/call response
dropped after the tool commits), same two protocol calls, same retry
policy (exactly one application retry on client-perceived timeout with
identical arguments and a fresh JSON-RPC id) -- plus a stable
application-level idempotency key: logical_operation_id is passed as a
tool argument and the tool's effect layer dedups on it, absorbing the
retry so only 1 external effect is committed.

Claim boundary (hard rule): MCP did not dedup; the application effect
layer did. This is one mitigation, not the only possible design.
tools/call defines no normative retry or duplicate-effect semantics for
this case. Applications can mitigate using stable idempotency at the
effect boundary; nothing here establishes any particular key or design
as required.

Expected result: 1 external effect for 1 logical operation.

Run:
    pip install -r requirements.txt   # mcp==2.2.0, Python 3.12
    python b1_positive_control.py
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import anyio

from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams
from mcp_types.jsonrpc import JSONRPCError, JSONRPCRequest, JSONRPCResponse

READ_TIMEOUT_SECS = 5.0
SERVER_SHUTDOWN_GRACE_SECS = 2.0

# -- inline append-only effect ledger: the authoritative source of truth ----
LEDGER: list[dict[str, Any]] = []


def commit_effect(
    *, logical_operation_id: str, item: str, quantity: int, attempt_id: str
) -> dict[str, str]:
    """Commit one external effect, deduping on the stable logical operation
    id at the application effect layer. A repeat execution for the same
    logical operation reuses the original effect id and appends no row."""
    for record in LEDGER:
        if record["logical_operation_id"] == logical_operation_id:
            return {"effect_id": record["effect_id"], "result": "deduped"}
    effect_id = uuid.uuid4().hex
    LEDGER.append(
        {
            "effect_id": effect_id,
            "logical_operation_id": logical_operation_id,
            "item": item,
            "quantity": quantity,
            "attempt_id": attempt_id,
            "result": "committed",
        }
    )
    return {"effect_id": effect_id, "result": "committed"}


def effect_count() -> int:
    """Authoritative effect count: rows in the ledger, nothing else."""
    return len(LEDGER)


# -- mutating tool -----------------------------------------------------------
def build_server() -> MCPServer:
    srv = MCPServer(name="b1-minimal-pc")

    @srv.tool()
    def create_order(
        logical_operation_id: str,
        item: str,
        quantity: int,
        attempt_id: str,
        ctx: Context,
    ) -> str:
        # The idempotency key travels as an ordinary tool argument; the
        # dedup decision happens at the application effect layer, below.
        outcome = commit_effect(
            logical_operation_id=logical_operation_id,
            item=item,
            quantity=quantity,
            attempt_id=attempt_id,
        )
        return json.dumps(outcome)

    return srv


def _tool_text(result: Any) -> str:
    for block in result.content:
        if block.type == "text":
            return block.text
    raise AssertionError(f"no text content in tool result: {result!r}")


async def main() -> None:
    server = build_server()
    streams_cm = create_client_server_memory_streams()
    client_streams, server_streams = await streams_cm.__aenter__()
    c_read, c_write = client_streams
    s_read, s_write = server_streams

    c2r_send, c2r_recv = anyio.create_memory_object_stream(256)
    r2c_send, r2c_recv = anyio.create_memory_object_stream(256)

    tools_call_ids: list[Any] = []
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
        """Same fault hook as the baseline: drop the attempt-1 tools/call
        response after the tool commits."""
        dropped = False
        async with c_read, r2c_send:
            async for msg in c_read:
                if (
                    not dropped
                    and isinstance(msg, SessionMessage)
                    and isinstance(msg.message, (JSONRPCResponse, JSONRPCError))
                    and tools_call_ids
                    and msg.message.id == tools_call_ids[0]
                ):
                    dropped = True
                    print(
                        "[fault] dropped attempt-1 tools/call response after commit "
                        f"(jsonrpc_id={msg.message.id!r}); client will perceive a timeout"
                    )
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
        print(f"negotiated protocolVersion: {init.protocol_version}")

        logical_operation_id = f"b1-pc-{uuid.uuid4().hex[:8]}"
        for attempt in (1, 2):
            args = {
                "logical_operation_id": logical_operation_id,
                "item": "widget",
                "quantity": 2,
                "attempt_id": f"attempt-{attempt}",
            }
            try:
                res = await session.call_tool(
                    "create_order", args, read_timeout_seconds=READ_TIMEOUT_SECS
                )
            except MCPError as exc:
                print(f"attempt {attempt}: client-perceived timeout ({type(exc).__name__})")
                continue
            payload = json.loads(_tool_text(res))
            print(
                f"attempt {attempt}: completed "
                f"(effect_id={payload['effect_id'][:8]}..., result={payload['result']})"
            )

        print(f"attempt-1 JSON-RPC id: {tools_call_ids[0]!r}")
        print(f"attempt-2 JSON-RPC id: {tools_call_ids[1]!r}")
        print(f"authoritative effect count: {effect_count()}")

        await session.__aexit__(None, None, None)
        await c2r_send.aclose()
        with anyio.move_on_after(SERVER_SHUTDOWN_GRACE_SECS):
            await server_done.wait()
        tg.cancel_scope.cancel()

    await streams_cm.__aexit__(None, None, None)

    # MCP did not dedup; the application effect layer did. One mitigation,
    # not the only possible design.
    if effect_count() == 1:
        print(
            "POSITIVE CONTROL: 1 external effect for 1 logical operation "
            "(retry absorbed by application-level dedup)"
        )
    else:
        print(f"POSITIVE CONTROL UNEXPECTED: effect count is {effect_count()}, expected 1")


if __name__ == "__main__":
    anyio.run(main)
