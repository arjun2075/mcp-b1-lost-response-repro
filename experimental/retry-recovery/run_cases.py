"""Real MCP 2.2.0 memory-stream calls, synthetic refund provider.

The original cases use harness policy flags only. The approval cases verify
real signed PIC approvals at the pinned SDK through pic_adapter's worker; the
provider, recovery store and caller authorization remain synthetic fixture
components.

Install the root requirements, plus the pinned PIC SDK for the approval cases
as described in README.md (PIC approval setup). Assertions fail the process.
No SDK automatic retry, real payments, persistence or LLM involvement.
"""
from __future__ import annotations

import sys

# The approval cases load the vendored signing helper; never write bytecode
# into the immutable vendored tree, even without python -B.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import importlib.metadata  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402

import anyio  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402
from mcp.shared.memory import SessionMessage, create_client_server_memory_streams  # noqa: E402
from mcp_types.jsonrpc import JSONRPCRequest, JSONRPCResponse, JSONRPCError  # noqa: E402

from pic_adapter.client import VerifierClient  # noqa: E402
from pic_adapter.verify import CONTRACT, EXAMPLE_COMMIT, SDK_COMMIT, VENDOR_DIR  # noqa: E402
from recovery import Context, Engine, reported_outcome  # noqa: E402

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

# Approval cases use a larger SDK read timeout than the original 0.25 s, sized
# for worker verification (about 5 ms per request observed). These constants
# are not a timing guarantee: unexpected verification delays or failures fail
# the scenario, and only a recorded response-drop event qualifies as the
# intended lost-response timeout.
APPROVAL_READ_TIMEOUT_S = 2.0
VERIFY_TIMEOUT_S = 1.5
EXPIRED_AT = "2099-01-01T00:00:01Z"   # one second after the fixture approval expires
BASE_ARGS = {"operation_id": "operation-1", "payment_id": "pay_A",
             "amount_minor": 4000, "currency": "EUR"}
ALLOW = ("allow", None)
DROPPED = None   # expected status of an attempt whose response is deliberately dropped


def attempt(approval, operation="operation-1", amount_minor=None, **context):
    changes = {"operation_id": operation}
    if amount_minor is not None:
        changes["amount_minor"] = amount_minor
    return approval, context, changes


def case(matrix, first, second, effects, statuses, adapter, error=False,
         mismatch=None, bound=None):
    """mismatch: expected decision_mismatch_observed per verification (default: none).
    bound: expected final decision mapping {decision_id: operation_id}, if asserted."""
    return {"matrix": matrix, "first": first, "second": second, "effects": effects,
            "statuses": statuses, "adapter": adapter, "error": error,
            "drop": statuses[0] is DROPPED,
            "mismatch": [False] * len(adapter) if mismatch is None else mismatch,
            "bound": bound}


APPROVAL_CASES = {
    "approval_stable_identity_retry": case(
        "R01 with real approval", attempt("valid"), attempt("valid"),
        [1, 1], [DROPPED, "completed"], [ALLOW, ALLOW]),
    "approval_recovery_without_fresh_approval": case(
        "R01 recovery with no approval presented", attempt("valid"), attempt(None),
        [1, 1], [DROPPED, "completed"], [ALLOW]),
    "approval_expired_recovery_allowed": case(
        "A01", attempt("valid"), attempt("expired"),
        [1, 1], [DROPPED, "completed"], [ALLOW, ("deny", "approval_expired")]),
    "approval_expired_disclosure_denied": case(
        "A02", attempt("valid"), attempt("expired", disclose=False),
        [1, 1], [DROPPED, "denied"], [ALLOW, ("deny", "approval_expired")]),
    "approval_execution_withdrawn_disclosure_allowed": case(
        "A04", attempt("valid"), attempt("valid", execute=False),
        [1, 1], [DROPPED, "completed"], [ALLOW, ALLOW]),
    "approval_execution_withdrawn_disclosure_denied": case(
        "A04", attempt("valid"), attempt("valid", execute=False, disclose=False),
        [1, 1], [DROPPED, "denied"], [ALLOW, ALLOW]),
    "approval_other_tenant": case(
        "A05", attempt("valid"), attempt("valid", tenant="tenant-b"),
        [1, 1], [DROPPED, "denied"], [ALLOW, ALLOW]),
    "approval_other_principal": case(
        "A05", attempt("valid"), attempt("valid", principal="principal-b"),
        [1, 1], [DROPPED, "denied"], [ALLOW, ALLOW]),
    "approval_changed_args_conflict": case(
        "A06", attempt("valid"), attempt("changed_args", amount_minor=5000),
        [1, 1], [DROPPED, "conflict"], [ALLOW, ALLOW]),
    "approval_tampered_new_operation": case(
        "A06 tampering control", attempt("valid"), attempt("tampered", operation="operation-2"),
        [1, 1], [DROPPED, "denied"], [ALLOW, ("deny", "signature_or_binding_rejected")]),
    "approval_reissued_recovery": case(
        "A07", attempt("valid"), attempt("reissued"),
        [1, 1], [DROPPED, "completed"], [ALLOW, ALLOW]),
    "approval_retained_decision_mismatch": case(
        "A07 negative: different verified decision for a retained operation",
        attempt("valid"), attempt("second_decision"),
        [1, 1], [DROPPED, "conflict"], [ALLOW, ALLOW],
        mismatch=[False, True], bound={"decision-001": "operation-1"}),
    "approval_replay_fresh_operation": case(
        "replay control (provisional: conflict)", attempt("valid"),
        attempt("valid", operation="operation-2"),
        [1, 1], [DROPPED, "conflict"], [ALLOW, ALLOW]),
    "approval_second_decision_new_operation": case(
        "T04-style positive control", attempt("valid"),
        attempt("second_decision", operation="operation-2"),
        [1, 2], ["completed", "completed"], [ALLOW, ALLOW]),
    "approval_expired_new_effect_denied": case(
        "new effect under expired approval", attempt("expired"), attempt("expired"),
        [0, 0], ["denied", "denied"], [("deny", "approval_expired")] * 2),
    "approval_revoked_new_effect_denied": case(
        "new effect under revoked signer", attempt("revoked"), attempt("revoked"),
        [0, 0], ["denied", "denied"], [("deny", "signer_revoked")] * 2),
    "approval_commit_then_error": case(
        "R05 with real approval", attempt("valid"), attempt("valid"),
        [1, 1], ["execution_error", "unknown"], [ALLOW, ALLOW], error=True),
}

PENDING = {
    "A03": "revoked signer at recovery: current engine behavior covered by synthetic "
           "policy tests; adoption pending review; MCP scenario not run",
    "T01-T03": "pruning and retention: not implemented",
    "C01": "concurrency: not exercised",
    "durability": "restart and crash recovery: not exercised",
}


def diagnostic_sha256(value) -> str:
    """Diagnostic digest for result records: SHA-256 of json.dumps(value,
    sort_keys=True, separators=(",", ":"), ensure_ascii=True). Distinct from,
    and not a substitute for, PIC's signed canonical (PIC-CJSON) digests."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def display_path(path: str) -> str:
    """Path relative to this directory when inside it; otherwise only the file name."""
    p = Path(path).absolute()
    try:
        return p.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return p.name


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


def approval_requests() -> dict:
    """Signed test proposals and verification contexts, keyed by approval name.

    Built with the vendored signing helper loaded under a private module name;
    the vendored directory is never put on sys.path here. Verification happens
    only in the pic_adapter worker.
    """
    spec = importlib.util.spec_from_file_location("_vendored_sign_and_verify",
                                                  VENDOR_DIR / "sign_and_verify.py")
    signing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signing)
    decision = json.loads((VENDOR_DIR / "fixtures" / "merchant_decision.json").read_text("utf-8"))
    assert decision["action"]["args"] == {k: BASE_ARGS[k] for k in
                                          ("payment_id", "amount_minor", "currency")}

    def signed(**changes):
        d = copy.deepcopy(decision)
        d.update(changes)
        return signing.build_signed_proposal(merchant_decision=d)

    def context(keyring="trusted", clock=None):
        ctx = {"keyring_fixture": keyring, "profile": "refund-approval-v1"}
        if clock:
            ctx["clock"] = clock
        return ctx

    tampered = signed()
    tampered["action"]["args"]["amount_minor"] = 9999
    changed = signed(action={"tool": "refund",
                             "args": {**decision["action"]["args"], "amount_minor": 5000}})
    return {
        "valid": (signed(), context()),
        "expired": (signed(), context(clock=EXPIRED_AT)),
        "revoked": (signed(), context("revoked")),
        "reissued": (signed(approval_ref="approval-002"), context()),
        "second_decision": (signed(decision_id="decision-002", approval_ref="approval-201"),
                            context()),
        "changed_args": (changed, context()),
        "tampered": (tampered, context()),
    }


def verification_role(engine, scope, retained, events_before) -> tuple[str, bool]:
    """Label how this attempt's verification result met the engine (label only).

    Fresh approval is not required for retained recovery; a presented, currently
    verified decision is checked for consistency. Only a mismatch emits an event,
    so the flag records an observed mismatch, not the absence of a check.
    """
    mismatch = any(e["event"] == "approval_decision_mismatch"
                   for e in engine.events[events_before:])
    if retained:
        return "retained_operation_verification", mismatch
    if any(k[3] == scope[3] and k[:3] != scope[:3] for k in engine.records):
        return "context_rejected_before_approval", mismatch
    return "admission_check", mismatch


async def run_approval_case(name, spec, verifier, approvals):
    engine = Engine(post_commit_error=spec["error"], require_approval=True)
    server = MCPServer(name="experimental-retry-recovery-approval")
    drop = spec["drop"]
    state = {"attempt": 0, "context": Context(), "approval": None}
    verifications = []

    @server.tool()
    async def refund(operation_id: str, payment_id: str, amount_minor: int, currency: str) -> str:
        # Capture this attempt's harness-owned context before awaiting verification.
        number, base, presented = state["attempt"], state["context"], state["approval"]
        approval, entry = None, None
        if presented is not None:
            proposal, verification_context = presented
            request = {"proposal": proposal,
                       "expected_action": {"tool": "refund", "impact": "money",
                                           "args": {"payment_id": payment_id,
                                                    "amount_minor": amount_minor,
                                                    "currency": currency}},
                       "verification_context": verification_context}
            started = time.perf_counter()
            approval = await verifier.verify(request)
            entry = {"attempt": number, "latency_s": round(time.perf_counter() - started, 6),
                     "decision": approval["decision"], "reason_code": approval["reason_code"],
                     "checks": approval["checks"],
                     "request_sha256": diagnostic_sha256(request),
                     "response_sha256": diagnostic_sha256(approval)}
            verifications.append(entry)
        # Install the complete attempt context, then call the engine with no await between.
        engine.context = replace(base, approval=approval)
        scope = (base.tenant, base.principal, "refund", operation_id)
        retained, events_before = scope in engine.records, len(engine.events)
        outcome = engine.call(operation_id, payment_id, amount_minor, currency)
        if entry is not None:
            entry["role"], entry["decision_mismatch_observed"] = verification_role(
                engine, scope, retained, events_before)
        return json.dumps(outcome)

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
                for number, (approval_key, context, changes) in enumerate(
                        (spec["first"], spec["second"]), start=1):
                    state["attempt"] = number
                    state["context"] = Context(**context)
                    state["approval"] = approvals[approval_key] if approval_key else None
                    args = {**BASE_ARGS, **changes}
                    try:
                        result = await session.call_tool(
                            "refund", args, read_timeout_seconds=APPROVAL_READ_TIMEOUT_S)
                    except MCPError as exc:
                        # Only a recorded response-drop event qualifies as the intended timeout.
                        assert drop and number == 1 and len(faults) == 1, str(exc)
                        assert "timed out" in str(exc).lower() or "timeout" in str(exc).lower(), str(exc)
                        obs = {"kind": "timeout", "exception": type(exc).__name__}
                    else:
                        assert not result.is_error, result
                        texts = [b.text for b in result.content if b.type == "text"]
                        assert len(texts) == 1
                        obs = {"kind": "response", "payload": json.loads(texts[0])}
                    observations.append({"attempt": number, "approval": approval_key,
                                         "context": context, "arguments": args,
                                         "observation": obs,
                                         "client_reported_outcome": reported_outcome(obs),
                                         "provider_effect_count": len(engine.provider.effects)})
            tg.cancel_scope.cancel()

    assert len(ids) == 2 and ids[0] != ids[1], ids
    assert len(faults) == int(drop), faults
    adapter = [(v["decision"], v["reason_code"]) for v in verifications]
    assert adapter == spec["adapter"], adapter
    mismatches = [v["decision_mismatch_observed"] for v in verifications]
    assert mismatches == spec["mismatch"], verifications
    if spec["bound"] is not None:
        # The retained record and decision mapping keep the original decision;
        # no mapping is created for a rejected association.
        expected = {("tenant-a", "principal-a", "refund", d): op
                    for d, op in spec["bound"].items()}
        assert engine.decisions == expected, engine.decisions
        for (tenant, principal, tool, decision_id), op in expected.items():
            assert engine.records[(tenant, principal, tool, op)].decision_id == decision_id
    # Every attempt: provider effects, delivered status and client-reported outcome.
    assert len(observations) == len(spec["statuses"]) == len(spec["effects"]) == 2
    for obs, status, effects in zip(observations, spec["statuses"], spec["effects"]):
        assert obs["provider_effect_count"] == effects, (obs, effects)
        if status is DROPPED:
            assert drop and obs["attempt"] == 1 and obs["observation"]["kind"] == "timeout", obs
        else:
            assert obs["observation"]["kind"] == "response", obs
            assert obs["observation"]["payload"]["status"] == status, (obs, status)
        if status == "completed":
            # Each delivered receipt equals the independent provider record it reports.
            assert effects > 0
            assert obs["observation"]["payload"]["receipt"] == engine.provider.effects[effects - 1]
        # Denial, conflict, error or unknown is never reported as confirmed or as failure.
        assert obs["client_reported_outcome"] == (
            "confirmed" if status == "completed" else "unknown"), obs
    if spec["statuses"][0] == "execution_error":
        assert observations[0]["observation"]["payload"]["outcome"] == "unknown"
    assert len(engine.provider.effects) == spec["effects"][-1]
    if "completed" not in spec["statuses"]:
        assert "fixture-receipt-" not in "".join(visible)
    return {"id": name, "matrix": spec["matrix"], "passed": True,
            "protocol_version": init.protocol_version, "jsonrpc_ids": ids,
            "transport_faults": faults, "attempts": observations,
            "adapter_results": verifications,
            "effect_count": len(engine.provider.effects), "engine_events": engine.events,
            "scope": ("real MCP memory streams; real pinned PIC approval verification in a "
                      "worker; synthetic in-process provider/recovery policy; fresh approval "
                      "is not required for retained recovery; a presented, currently verified "
                      "decision is checked for consistency")}


async def run_all():
    original = [await run_case(name, spec) for name, spec in CASES.items()]
    approvals = approval_requests()
    async with VerifierClient(timeout=VERIFY_TIMEOUT_S) as verifier:
        approval = [await run_approval_case(name, spec, verifier, approvals)
                    for name, spec in APPROVAL_CASES.items()]
        ready = dict(verifier.ready_info or {})
    worker = {"python": display_path(verifier.python), "ready": ready, "starts": verifier.starts,
              "terminations": list(verifier.terminations),
              "cleanup_errors": list(verifier.cleanup_errors),
              "unconfirmed_processes": len(verifier.unconfirmed_processes),
              "verifications": len(verifier.latencies)}
    # Every approval case must have used one healthy worker without replacement.
    assert worker["starts"] == 1 and not worker["terminations"], worker
    assert not worker["cleanup_errors"] and not worker["unconfirmed_processes"], worker
    assert ready.get("installed_pic_sdk_commit") == SDK_COMMIT, ready
    return original, approval, worker


def environment_freeze() -> dict:
    proc = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True,
                          text=True, timeout=120, check=True)
    lines = sorted(line for line in proc.stdout.splitlines() if line.strip())
    return {"python_executable": display_path(sys.executable), "pip_freeze": lines,
            "pip_freeze_sha256": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not __debug__:
        # Scenario validation uses assert statements, which -O / PYTHONOPTIMIZE remove;
        # refuse rather than report unchecked results as passed.
        print("run_cases.py refuses to run with assertions disabled "
              "(python -O or PYTHONOPTIMIZE); no new results were written", file=sys.stderr)
        raise SystemExit(2)
    assert importlib.metadata.version("mcp") == "2.2.0"
    results, approval_results, worker = anyio.run(run_all)
    here = Path(__file__).resolve().parent
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [Path(__file__), Path(__file__).with_name("recovery.py")]}
    for rel in ("pic_adapter/clock.py", "pic_adapter/verify.py", "pic_adapter/worker.py",
                "pic_adapter/client.py", "pic_adapter/keyrings/trusted.json",
                "pic_adapter/keyrings/revoked.json", "pic_adapter/keyrings/empty.json",
                "pic_adapter/keyrings/expiring.json", "pic_adapter/vendor/MANIFEST.json"):
        hashes[rel] = hashlib.sha256((here / rel).read_bytes()).hexdigest()
    report = {"python": platform.python_version(), "mcp": importlib.metadata.version("mcp"),
              "mcp_types": importlib.metadata.version("mcp-types"),
              "source_sha256": hashes, "environment": environment_freeze(),
              "pic_adapter": {"contract": CONTRACT, "example_commit": EXAMPLE_COMMIT,
                              "pic_sdk_commit": SDK_COMMIT,
                              "installed_pic_sdk_commit": worker["ready"].get(
                                  "installed_pic_sdk_commit"),
                              "approval_read_timeout_s": APPROVAL_READ_TIMEOUT_S,
                              "verify_timeout_s": VERIFY_TIMEOUT_S,
                              "diagnostic_digest": diagnostic_sha256.__doc__.split("\n\n")[0]
                              .replace("\n", " "),
                              "worker": worker},
              "results": results, "passed": len(results), "total": len(CASES),
              "approval_results": approval_results, "approval_passed": len(approval_results),
              "approval_total": len(APPROVAL_CASES), "pending": PENDING}
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(f"{len(results)}/{len(CASES)} real-MCP experimental cases passed")
    print(f"{len(approval_results)}/{len(APPROVAL_CASES)} real-MCP approval cases passed "
          "(real pinned PIC verification)")


if __name__ == "__main__":
    main()
