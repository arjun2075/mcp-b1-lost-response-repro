"""Real MCP 2.2.0 memory-stream calls, synthetic refund provider; no PIC yet.

Run with the repository's requirements installed. Assertions fail the process.
No SDK automatic retry, real payments, persistence or LLM involvement.
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
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams
from mcp_types.jsonrpc import JSONRPCRequest, JSONRPCResponse, JSONRPCError

from recovery import Context, Engine, reported_outcome

CASES = {
    "unguarded_lost_response": (True, False, False, True, "same", 2, "completed"),
    "stable_identity_retry": (False, False, False, True, "same", 1, "completed"),
    "intentional_repeat": (False, False, False, True, "new", 2, "completed"),
    "commit_then_error": (False, True, False, False, "same", 1, "unknown"),
    "unsafe_success_only_cache": (False, True, True, False, "same", 2, "completed"),
    "result_disclosure_denied": (False, False, False, True, "deny_disclosure", 1, "denied"),
    "execution_revoked_recovery_allowed": (False, False, False, True, "revoke_execution", 1, "completed"),
    "different_tenant_rejected": (False, False, False, True, "tenant", 1, "denied"),
    "same_identity_changed_args": (False, False, False, True, "mismatch", 1, "conflict"),
    "fresh_key_each_attempt": (False, False, False, True, "new", 2, "completed"),
}


async def run_case(name, spec):
    unguarded, error, evict, drop, second, expected_count, expected_status = spec
    engine = Engine(unguarded=unguarded, post_commit_error=error,
                    unsafe_error_eviction=evict)
    server = MCPServer(name="experimental-retry-recovery")

    @server.tool()
    def refund(operation_id: str, payment_id: str, amount_minor: int, currency: str) -> str:
        return json.dumps(engine.call(operation_id, payment_id, amount_minor, currency))

    ids, observations, visible, faults = [], [], [], []
    async with create_client_server_memory_streams() as (client, server_streams):
        c_read, c_write = client
        s_read, s_write = server_streams
        req_send, req_recv = anyio.create_memory_object_stream(256)
        out_send, out_recv = anyio.create_memory_object_stream(256)

        async def request_relay():
            async with req_recv, c_write:
                async for msg in req_recv:
                    if isinstance(msg, SessionMessage) and isinstance(msg.message, JSONRPCRequest):
                        if msg.message.method == "tools/call":
                            ids.append(msg.message.id)
                    await c_write.send(msg)

        async def response_relay():
            async with c_read, out_send:
                async for msg in c_read:
                    rpc = msg.message
                    if (drop and not faults and ids and
                            isinstance(rpc, (JSONRPCResponse, JSONRPCError)) and rpc.id == ids[0]):
                        assert len(engine.provider.effects) == 1, "drop must follow effect commit"
                        assert isinstance(rpc, JSONRPCResponse), "do not mistake protocol error for completion"
                        faults.append({"event": "response_dropped_after_commit", "jsonrpc_id": rpc.id})
                        continue
                    if isinstance(rpc, (JSONRPCResponse, JSONRPCError)) and rpc.id in ids:
                        # Includes all delivered structured/error/text fields for leak checks.
                        visible.append(rpc.model_dump_json())
                    await out_send.send(msg)

        async def serve():
            low = server._lowlevel_server
            await low.run(s_read, s_write, low.create_initialization_options(),
                          raise_exceptions=False)

        async with anyio.create_task_group() as tg:
            tg.start_soon(serve)
            tg.start_soon(request_relay)
            tg.start_soon(response_relay)
            async with ClientSession(out_recv, req_send) as session:
                init = await session.initialize()
                for attempt in (1, 2):
                    if attempt == 2:
                        if second == "deny_disclosure":
                            engine.context = Context(disclose=False)
                        elif second == "revoke_execution":
                            engine.context = Context(execute=False)
                        elif second == "tenant":
                            engine.context = Context(tenant="tenant-b")
                    args = {"operation_id": "operation-1", "payment_id": "payment-1",
                            "amount_minor": 100, "currency": "EUR"}
                    if attempt == 2 and second == "new":
                        args["operation_id"] = "operation-2"
                    if attempt == 2 and second == "mismatch":
                        args["amount_minor"] = 200
                    try:
                        result = await session.call_tool("refund", args, read_timeout_seconds=0.25)
                    except MCPError as exc:
                        # Only accept the deliberately dropped response as a timeout.
                        assert drop and attempt == 1 and len(faults) == 1, str(exc)
                        assert "timed out" in str(exc).lower() or "timeout" in str(exc).lower(), str(exc)
                        obs = {"kind": "timeout", "exception": type(exc).__name__}
                    else:
                        assert not result.is_error, result
                        texts = [b.text for b in result.content if b.type == "text"]
                        assert len(texts) == 1
                        obs = {"kind": "response", "payload": json.loads(texts[0])}
                    observations.append({"attempt": attempt, "observation": obs,
                                         "client_reported_outcome": reported_outcome(obs),
                                         "provider_effect_count": len(engine.provider.effects)})
            tg.cancel_scope.cancel()

    assert len(ids) == 2 and ids[0] != ids[1], ids
    assert len(faults) == int(drop), faults
    assert len(engine.provider.effects) == expected_count
    last = observations[-1]
    assert last["observation"]["payload"]["status"] == expected_status
    assert last["client_reported_outcome"] == ("confirmed" if expected_status == "completed" else "unknown")
    if drop:
        assert observations[0]["observation"]["kind"] == "timeout"
        assert observations[0]["provider_effect_count"] == 1
    if second in ("deny_disclosure", "tenant", "mismatch"):
        assert "fixture-receipt-" not in "".join(visible)
    if name == "stable_identity_retry":
        assert last["observation"]["payload"]["receipt"] == engine.provider.effects[0]
    return {"id": name, "passed": True, "protocol_version": init.protocol_version,
            "jsonrpc_ids": ids, "transport_faults": faults, "attempts": observations,
            "effect_count": len(engine.provider.effects), "engine_events": engine.events,
            "scope": "real MCP memory streams; synthetic in-process provider/recovery policy"}


async def run_all():
    return [await run_case(name, spec) for name, spec in CASES.items()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assert importlib.metadata.version("mcp") == "2.2.0"
    results = anyio.run(run_all)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [Path(__file__), Path(__file__).with_name("recovery.py")]}
    report = {"python": platform.python_version(), "mcp": importlib.metadata.version("mcp"),
              "mcp_types": importlib.metadata.version("mcp-types"),
              "source_sha256": hashes, "pic_adapter": "not implemented; no signed approval verification",
              "results": results, "passed": len(results), "total": len(CASES)}
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(f"{len(results)}/{len(CASES)} real-MCP experimental cases passed")


if __name__ == "__main__":
    main()
