"""C01 overlapping real-MCP calls with gated synthetic effects.

The MCP client/server share memory streams, but requests overlap while the
first synthetic provider commit is held. No sleeps and no reliance on timing
for admission order. Safe reservations and an unsafe check-then-act control
are compared under the same fixture. Does not test remote payments, durability,
thread/multi-process synchronization or restart recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams
from mcp_types.jsonrpc import JSONRPCRequest

from concurrency import ConcurrentEngine

HERE = Path(__file__).resolve().parent
ARGS = {"operation_id": "operation-1", "payment_id": "pay_A", "amount_minor": 4000,
        "currency": "EUR"}
TIMEOUT_SECONDS = 10.0


async def run_case(name: str, *, unsafe=False, post_commit_error=False,
                   second_args=None) -> dict:
    engine = ConcurrentEngine(unsafe=unsafe, post_commit_error=post_commit_error)
    server = MCPServer(name="c01-concurrent-retry-fixture")

    @server.tool()
    async def refund(operation_id: str, payment_id: str,
                     amount_minor: int, currency: str) -> str:
        return json.dumps(await engine.call(operation_id, payment_id, amount_minor, currency))

    requests = []
    responses = {}
    done = {name: anyio.Event() for name in ("first", "second", "recovery")}
    client_message_ids = []

    async with create_client_server_memory_streams() as (client, streams):
        c_read, c_write = client
        s_read, s_write = streams
        req_send, req_recv = anyio.create_memory_object_stream(256)
        out_send, out_recv = anyio.create_memory_object_stream(256)

        async def request_relay():
            async with req_recv, c_write:
                async for message in req_recv:
                    rpc = message.message
                    if isinstance(message, SessionMessage) and isinstance(rpc, JSONRPCRequest):
                        if rpc.method == "tools/call":
                            client_message_ids.append(rpc.id)
                    await c_write.send(message)

        async def response_relay():
            async with c_read, out_send:
                async for message in c_read:
                    await out_send.send(message)

        async def serve():
            low = server._lowlevel_server
            await low.run(s_read, s_write, low.create_initialization_options(),
                          raise_exceptions=False)

        async def invoke(session, label, arguments):
            try:
                reply = await session.call_tool("refund", arguments,
                                                read_timeout_seconds=TIMEOUT_SECONDS)
                assert not reply.is_error, (label, reply)
                text = [item.text for item in reply.content if item.type == "text"]
                assert len(text) == 1, (label, reply)
                responses[label] = json.loads(text[0])
                requests.append({"label": label, "arguments": arguments,
                                 "response": responses[label]})
            finally:
                done[label].set()

        with anyio.fail_after(TIMEOUT_SECONDS):
            async with anyio.create_task_group() as tg:
                tg.start_soon(serve)
                tg.start_soon(request_relay)
                tg.start_soon(response_relay)
                async with ClientSession(out_recv, req_send) as session:
                    init = await session.initialize()
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(invoke, session, "first", dict(ARGS))
                        # The first provider is held before it can commit.
                        await engine.provider.first_entered.wait()
                        assert engine.provider.effects == []
                        tasks.start_soon(invoke, session, "second", dict(second_args or ARGS))
                        independent_second = second_args is not None and second_args["operation_id"] != ARGS["operation_id"]
                        if unsafe or independent_second:
                            await engine.provider.second_entered.wait()
                            assert not done["second"].is_set()
                        else:
                            await done["second"].wait()
                        assert not done["first"].is_set()
                        assert len(engine.provider.effects) == 0
                        held = {"first_held": True,
                                "second_done_pre_release": done["second"].is_set(),
                                "dispatches_started_pre_release": engine.provider.attempts,
                                "effects_pre_release": len(engine.provider.effects)}
                        engine.provider.release.set()
                        await done["first"].wait()
                        await done["second"].wait()
                        tasks.start_soon(invoke, session, "recovery", dict(ARGS))
                        await done["recovery"].wait()
                tg.cancel_scope.cancel()

    assert len(client_message_ids) == 3, client_message_ids
    assert len(set(client_message_ids)) == 3, client_message_ids
    original = responses["first"]
    second = responses["second"]
    recovered = responses["recovery"]
    receipts = engine.provider.effects
    for row in receipts:
        assert row["args"] == {"payment_id": "pay_A", "amount_minor": 4000, "currency": "EUR"} or (
            second_args is not None and row["args"] == {
                "payment_id": second_args["payment_id"],
                "amount_minor": second_args["amount_minor"],
                "currency": second_args["currency"]}), row
    assert [x["receipt_marker"] for x in receipts] == [
        f"fixture-receipt-{i}" for i in range(1, len(receipts) + 1)]

    if unsafe:
        assert engine.provider.attempts == 2 and len(receipts) == 2
        assert original["status"] == second["status"] == "completed"
        assert original["receipt"] != second["receipt"]
        assert held["dispatches_started_pre_release"] == 2
        assert recovered["status"] == "completed"
    elif second_args and second_args["operation_id"] != ARGS["operation_id"]:
        assert engine.provider.attempts == len(receipts) == 2
        assert original["status"] == second["status"] == "completed"
        assert original["receipt"] != second["receipt"]
        assert recovered == original
    elif second_args:
        assert engine.provider.attempts == len(receipts) == 1
        assert second == {"status": "conflict"}
        assert original["status"] == "completed" and recovered == original
    elif post_commit_error:
        assert engine.provider.attempts == len(receipts) == 1
        assert original == {"status": "execution_error", "outcome": "unknown"}
        assert second == recovered == {"status": "unknown", "reason": "in_progress_or_indeterminate"}
    else:
        assert engine.provider.attempts == len(receipts) == 1
        assert held["dispatches_started_pre_release"] == 1
        assert second == {"status": "unknown", "reason": "in_progress_or_indeterminate"}
        assert original["status"] == "completed" and recovered == original

    return {"name": name, "passed": True, "protocol_version": init.protocol_version,
            "jsonrpc_ids": client_message_ids, "gate": held,
            "requests": requests, "provider_effects": receipts,
            "provider_attempts": engine.provider.attempts,
            "events": engine.events,
            "scope": "real MCP memory streams; in-process synthetic provider and admission"}


async def run_all():
    return [
        await run_case("C01_reserved_overlapping_retry"),
        await run_case("C01_unlocked_check_then_act_counterexample", unsafe=True),
        await run_case("C01_commit_then_error_unknown", post_commit_error=True),
        await run_case("C01_inflight_changed_arguments_conflict", second_args={**ARGS, "amount_minor": 5000}),
        await run_case("C01_independent_operation_positive_control", second_args={**ARGS, "operation_id": "operation-2"}),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        raise SystemExit("refusing C01 scenario run with assertions disabled")
    cases = anyio.run(run_all)
    data = {"python": platform.python_version(), "mcp": importlib.metadata.version("mcp"),
            "mcp_types": importlib.metadata.version("mcp-types"),
            "source_sha256": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                              for name in ("concurrency.py", "run_concurrency_cases.py")},
            "scope": "single-process in-memory MCP; no remote effect, persistence, restart or replica safety",
            "passed": len(cases), "total": 5, "cases": cases}
    assert len(cases) == 5
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(cases)}/5 overlapping real-MCP scenarios passed")


if __name__ == "__main__":
    main()
