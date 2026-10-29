"""Real MCP / pinned PIC cases for retention and non-authoritative readback.

Synthetic sequential provider/store/caller policy, scripted client, no model or
live payment provider. Run from this directory with the README environment.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib.util
import json
import platform
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams
from mcp_types.jsonrpc import JSONRPCRequest, JSONRPCResponse, JSONRPCError

from pic_adapter.client import VerifierClient
from pic_adapter.verify import SDK_COMMIT, VENDOR_DIR
from recovery import Context, Engine, reported_outcome
from retention import RetentionEngine
from run_cases import BASE_ARGS, environment_freeze

DAY = 86400
EPOCH = datetime(2026, 10, 29, tzinfo=timezone.utc)
SOURCE = {"repository": "https://github.com/terminalstate/double-take",
          "commit": "3bfd494293e70cb5a4eb068e005d76739fd760aa",
          "trace": "runs-l2/run.jsonl", "line": 142,
          "selector": {"scenario": "paypal_refund", "model": "claude-sonnet-5",
                       "prompt": "plain", "trial": 2}}


def refund(status, effects=1, approval="valid", **changes):
    return {"tool": "refund", "status": status, "effects": effects,
            "approval": approval, **changes}


def snapshot(engine):
    if isinstance(engine, RetentionEngine):
        return engine.snapshot()
    return {"receipt_records": len(engine.records),
            "provider_effect_count": len(engine.provider.effects)}


def retention_approvals():
    spec = importlib.util.spec_from_file_location("_retention_signing", VENDOR_DIR / "sign_and_verify.py")
    signing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signing)
    baseline = json.loads((VENDOR_DIR / "fixtures/merchant_decision.json").read_text())
    def signed(days=7, **changes):
        return signing.build_signed_proposal(merchant_decision={**baseline,
            "expires_at": (EPOCH + timedelta(days=days)).isoformat().replace("+00:00", "Z"), **changes})
    return {"valid": signed(), "expired": signed(days=1),
            "reissued": signed(days=14, approval_ref="approval-002"),
            "second_decision": signed(days=14, approval_ref="approval-201", decision_id="decision-002")}


async def exercise(name, engine, steps, verifier, approvals, *, drop_first=True):
    server = MCPServer(name="retention-readback-fixture")
    state = {"step": None}
    verifications, ids, visible, faults, observations = [], [], [], [], []

    @server.tool()
    async def refund(operation_id: str, payment_id: str, amount_minor: int,
                     currency: str, claimed_created_at: int | None = None) -> str:
        step = state["step"]
        base = Context(**step.get("context", {}))
        approval = None
        if engine.require_approval:
            proposal = approvals[step["approval"]]
            ctx = {"keyring_fixture": "trusted", "profile": "refund-approval-v1",
                   "clock": (EPOCH + timedelta(seconds=engine.clock.now)).isoformat().replace("+00:00", "Z")}
            approval = await verifier.verify({
                "proposal": proposal, "verification_context": ctx,
                "expected_action": {"tool": "refund", "impact": "money",
                                    "args": {"payment_id": payment_id,
                                             "amount_minor": amount_minor,
                                             "currency": currency}}})
            verifications.append({"decision": approval["decision"],
                                  "reason_code": approval["reason_code"],
                                  "verification_context": ctx,
                                  "checks": approval["checks"]})
            expected = ("deny", "approval_expired") if step["approval"] == "expired" else ("allow", None)
            assert (approval["decision"], approval["reason_code"]) == expected
        engine.context = replace(base, approval=approval)
        args = {"operation_id": operation_id, "payment_id": payment_id,
                "amount_minor": amount_minor, "currency": currency}
        if isinstance(engine, RetentionEngine):
            args["claimed_created_at"] = claimed_created_at
        return json.dumps(engine.call(**args))

    @server.tool()
    def list_transactions(transaction_id: str) -> str:
        # A synthetic capture-only search, inspired by the pinned external trace.
        # The capture is found; this query does not correlate its separate refunds.
        return json.dumps({"found": True, "transaction_details": [
            {"transaction_info": {"transaction_id": transaction_id,
                                  "transaction_event_code": "T0006"}}],
            "total_items": 1})

    async with create_client_server_memory_streams() as (client, streams):
        c_read, c_write = client
        s_read, s_write = streams
        req_send, req_recv = anyio.create_memory_object_stream(256)
        out_send, out_recv = anyio.create_memory_object_stream(256)

        async def requests():
            async with req_recv, c_write:
                async for msg in req_recv:
                    if isinstance(msg, SessionMessage) and isinstance(msg.message, JSONRPCRequest):
                        if msg.message.method == "tools/call":
                            ids.append(msg.message.id)
                    await c_write.send(msg)

        async def responses():
            async with c_read, out_send:
                async for msg in c_read:
                    rpc = msg.message
                    if isinstance(rpc, (JSONRPCResponse, JSONRPCError)) and rpc.id in ids:
                        if drop_first and not faults and rpc.id == ids[0]:
                            assert isinstance(rpc, JSONRPCResponse)
                            assert len(engine.provider.effects) == 1
                            faults.append({"event": "response_dropped_after_commit", "id": rpc.id})
                            continue
                        visible.append(rpc.model_dump_json())
                    await out_send.send(msg)

        async def serve():
            low = server._lowlevel_server
            await low.run(s_read, s_write, low.create_initialization_options(),
                          raise_exceptions=False)

        async with anyio.create_task_group() as tg:
            tg.start_soon(serve)
            tg.start_soon(requests)
            tg.start_soon(responses)
            async with ClientSession(out_recv, req_send) as session:
                init = await session.initialize()
                for number, step in enumerate(steps, 1):
                    if "advance" in step:
                        engine.clock.advance(step["advance"])
                        engine.prune()
                    state["step"] = step
                    args = ({"transaction_id": "pay_A"} if step["tool"] == "list_transactions"
                            else {**BASE_ARGS, **step.get("arguments", {})})
                    before = len(engine.provider.effects)
                    events_before = len(engine.events)
                    try:
                        result = await session.call_tool(step["tool"], args, read_timeout_seconds=2.0)
                    except MCPError as exc:
                        assert drop_first and number == 1 and len(faults) == 1
                        assert "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
                        obs = {"kind": "timeout"}
                    else:
                        assert not result.is_error, result
                        texts = [b.text for b in result.content if b.type == "text"]
                        assert len(texts) == 1
                        obs = {"kind": "response", "payload": json.loads(texts[0])}
                    count = len(engine.provider.effects)
                    assert count == step["effects"], (name, step, count)
                    if number == 1 and drop_first:
                        assert obs["kind"] == "timeout"
                    elif step["tool"] == "refund":
                        assert obs["payload"]["status"] == step["status"], (name, obs)
                        if "reason" in step:
                            assert obs["payload"].get("reason") == step["reason"]
                        if step["status"] == "completed":
                            assert obs["payload"]["receipt"] == engine.provider.effects[count - 1]
                    else:
                        assert obs["payload"]["found"] is True
                        assert all(r["transaction_info"]["transaction_event_code"] == "T0006"
                                   for r in obs["payload"]["transaction_details"])
                    outcome = (reported_outcome(obs) if step["tool"] == "refund" else "unknown")
                    if step["tool"] == "list_transactions":
                        assert count == before == 1
                    if obs.get("payload", {}).get("status") != "completed":
                        assert outcome == "unknown"
                    observations.append({"step": number, "tool": step["tool"],
                                         "arguments": args, "approval": step.get("approval"),
                                         "observation": obs, "client_reported_outcome": outcome,
                                         "dispatch": "performed" if count > before else "refused_or_recovered",
                                         "store": snapshot(engine),
                                         "events": engine.events[events_before:]})
            tg.cancel_scope.cancel()
    assert len(ids) == len(steps) and len(set(ids)) == len(ids)
    assert len(faults) == int(drop_first)
    if not any(s.get("status") == "completed" for s in steps[1 if drop_first else 0:]):
        assert "fixture-receipt-" not in "".join(visible)
    return {"id": name, "passed": True, "protocol_version": init.protocol_version,
            "jsonrpc_ids": ids, "faults": faults, "observations": observations,
            "verifications": verifications, "events": engine.events,
            "final_store": snapshot(engine), "effect_count": len(engine.provider.effects)}


async def run_all():
    approvals = retention_approvals()
    results = []
    async with VerifierClient(timeout=1.5) as verifier:
        cases = {
            "T01_receipt_pruned_same_approval": [refund("unknown", advance=2 * DAY, reason="receipt_pruned")],
            "T01_expired_approval_tombstone": [refund("unknown", approval="expired", advance=2 * DAY)],
            "T02_reissued_approval_tombstone": [refund("unknown", approval="reissued", advance=2 * DAY)],
            "T02_new_decision_same_operation": [refund("conflict", approval="second_decision", advance=2 * DAY)],
            "T03_changed_claimed_creation_time": [refund("unknown", advance=2 * DAY,
                arguments={"claimed_created_at": 2 * DAY})],
            "T01_disclosure_denied": [refund("denied", advance=2 * DAY, context={"disclose": False})],
            "T01_other_principal": [refund("denied", advance=2 * DAY, context={"principal": "other"})],
            "T01_admission_evidence_expired": [refund("unknown", advance=7 * DAY, reason="admission_closed")],
            "T02_reissued_after_admission_expiry": [refund("unknown", approval="reissued", advance=7 * DAY)],
            "T03_timestamp_after_admission_expiry": [refund("unknown", advance=7 * DAY,
                arguments={"claimed_created_at": 7 * DAY})],
            "expiry_refuses_even_new_identity": [refund("unknown", approval="second_decision", advance=7 * DAY,
                arguments={"operation_id": "operation-2"}, reason="admission_closed")],
            "T04_new_operation_while_tombstone_retained": [refund("completed", effects=2,
                approval="second_decision", advance=2 * DAY, arguments={"operation_id": "operation-2"})],
            "before_receipt_horizon": [refund("completed", advance=DAY - 1)],
            "at_receipt_horizon": [refund("unknown", advance=DAY, reason="receipt_pruned")],
        }
        for name, tail in cases.items():
            engine = RetentionEngine(require_approval=True)
            result = await exercise(name, engine, [refund(None), *tail], verifier, approvals)
            elapsed = tail[0].get("advance", 0)
            if elapsed >= 7 * DAY:
                assert engine.admission_closed
                assert not engine.records and not engine.tombstones and not engine.decisions
                assert not engine.admitted_at
            elif elapsed >= DAY:
                assert len(engine.tombstones) == 1 and not engine.admission_closed
                assert next(iter(engine.tombstones.values())).receipt is None
                assert engine.admitted_at[("tenant-a", "principal-a", "refund", "operation-1")] == 0
                assert len(engine.records) == int(name.startswith("T04"))
            else:
                assert len(engine.records) == 1 and not engine.tombstones
            results.append(result)
        lookup = {"tool": "list_transactions", "effects": 1}
        for name, engine, last, effects in (
                ("negative_read_retained_unknown", RetentionEngine(post_commit_error=True), "unknown", 1),
                ("negative_read_pruned_receipt", RetentionEngine(post_commit_error=True), "unknown", 1),
                ("negative_read_unsafe_eviction_control", Engine(post_commit_error=True,
                    unsafe_error_eviction=True), "completed", 2)):
            first = refund("execution_error", approval=None)
            second = refund(last, effects=effects, approval=None)
            read = dict(lookup)
            if name == "negative_read_pruned_receipt":
                read["advance"] = 2 * DAY
            result = await exercise(name, engine, [first, read, second], verifier, approvals,
                                    drop_first=False)
            result["external_mapping"] = SOURCE
            results.append(result)
        ready = dict(verifier.ready_info)
        assert ready["installed_pic_sdk_commit"] == SDK_COMMIT
        assert verifier.starts == 1 and not verifier.terminations
        assert not verifier.cleanup_errors and not verifier.unconfirmed_processes
        worker = {"starts": verifier.starts, "verifications": len(verifier.latencies), "ready": ready}
    assert not verifier.cleanup_errors and not verifier.unconfirmed_processes
    return results, worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        raise SystemExit("refusing run with assertions disabled")
    results, worker = anyio.run(run_all)
    here = Path(__file__).resolve().parent
    sources = ["retention.py", "run_retention_cases.py", "recovery.py", "run_cases.py",
               "pic_adapter/verify.py", "pic_adapter/clock.py", "pic_adapter/client.py",
               "pic_adapter/worker.py", "pic_adapter/keyrings/trusted.json",
               "pic_adapter/vendor/MANIFEST.json"]
    report = {"python": platform.python_version(), "base_pr4_commit":
              "f01ccb479399798e68a2c4d4ad1ad0ef69e6e1d3",
              "source_sha256": {f: hashlib.sha256((here / f).read_bytes()).hexdigest() for f in sources},
              "environment": environment_freeze(), "worker": worker,
              "scope": "real MCP and pinned PIC; synthetic sequential retention and provider; scripted client",
              "external_mapping": SOURCE, "passed": len(results), "total": 17, "results": results}
    assert len(results) == report["total"]
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{len(results)}/17 retention/readback scenarios passed")


if __name__ == "__main__":
    main()
