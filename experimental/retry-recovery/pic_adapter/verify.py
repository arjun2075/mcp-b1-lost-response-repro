"""Non-dispatching PIC approval verifier for the retry-recovery fixture.

Implements the fixture-local contract "retry-recovery-draft-1" (see
../adapter-contract.md): verify a signed refund proposal with the pinned PIC
SDK and the vendored refund-approval profile, compare it with the harness's
expected action, and report what was established. It never calls a tool,
sink or callback. Execution, duplicate suppression, caller authorization and
receipt disclosure belong to the harness.

Reuses the vendored example's profile helpers (integration.py at eb7cd467)
in the order of its verify_and_dispatch steps 1 to 6. Steps 7 to 9
(recording and dispatch) are replaced by the expected-action comparison.

The vendored modules use top-level names (integration, sign_and_verify), so
they are imported only after pic_adapter/worker.py has put the vendored
directory on sys.path, and only if they resolve inside it. Elsewhere,
verify_approval() returns decision "error". A supplied test clock likewise
requires the worker's opt-in (see clock.py).

Check statuses:
  pass           established by the pipeline result or an independent observation
  fail           established as failed
  not_evaluated  not performed: skipped by the adapter after an earlier
                 rejection, or not applicable because its input was absent
  unavailable    the pipeline did not expose that stage's result

signer_trust is the configured keyring status of the canonical evidence
entry's key_id. It does not establish that the proposal was signed by that
key. Authorization depends only on the pipeline result, the profile checks
and the expected-action comparison. SDK diagnostic text is matched
internally only, to select an explanatory reason_code and, for pinned
messages covered by tests, to mark the specific failed stage. It is never
returned: trace carries the PIC error code and fixed explanatory text, so
caller-controlled values cannot be echoed back.

Test instrumentation for the retry-recovery fixture; not part of PIC or MCP.
"""
from __future__ import annotations

import contextlib
import copy
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

from pic_adapter.clock import ClockError, fixed_utc_now, parse_test_clock

CONTRACT = "retry-recovery-draft-1"
PROFILE = "refund-approval-v1"
EXAMPLE_COMMIT = "eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e"
SDK_COMMIT = "330fdd817ef81d43461ea1787939e0d1cc87d589"

HERE = Path(__file__).resolve().parent
VENDOR_DIR = HERE / "vendor" / "refund_approval_eb7cd467"
KEYRING_DIR = HERE / "keyrings"
KEYRINGS = {name: KEYRING_DIR / f"{name}.json"
            for name in ("trusted", "revoked", "empty", "expiring")}

CHECKS = ("signature", "proposal_binding", "signer_trust", "freshness",
          "workflow_profile", "expected_action")
REQUEST_KEYS = {"proposal", "expected_action", "verification_context"}
ACTION_KEYS = {"tool", "impact", "args"}
CONTEXT_REQUIRED = {"keyring_fixture", "profile"}
CONTEXT_ALLOWED = CONTEXT_REQUIRED | {"clock"}

# Pinned SDK diagnostics (pic_standard/evidence.py at 330fdd81), matched in
# full against messages for the canonical evidence entry only, and only when
# that entry's id is unique. Each pattern is exercised by test_pic_adapter.py.
DIAGNOSTICS = (
    (re.compile(r"attestation expired at \S+"), "freshness", "approval_expired"),
    (re.compile(r"(?:args|claims|intent)_digest mismatch \(key_id='[^']*'\)"),
     "proposal_binding", "signature_or_binding_rejected"),
    (re.compile(r"attestation (?:tool|impact) mismatch: .+"),
     "proposal_binding", "signature_or_binding_rejected"),
    (re.compile(r"attestation provenance_ids mismatch"),
     "proposal_binding", "signature_or_binding_rejected"),
    (re.compile(r"signature invalid \(key_id='[^']*'\)"),
     "signature", "signature_or_binding_rejected"),
)
KEY_STATUS_REASONS = {"revoked": "signer_revoked", "missing": "signer_untrusted",
                      "expired": "signer_untrusted"}

# Fixed explanatory text returned in trace. Never includes input values.
REASON_TEXT = {
    "approval_expired": "attestation expiry check failed at the verification instant",
    "signature_or_binding_rejected": "signature or signed-proposal binding check failed",
    "signer_revoked": "canonical evidence key_id is revoked in the configured keyring",
    "signer_untrusted": "canonical evidence key_id is missing or expired in the configured keyring",
    "evidence_rejected_unclassified": "evidence verification failed; failed stage not identified",
    "proposal_rejected": "PIC rejected the proposal outside the classified evidence stages",
    "profile_rejected": "proposal is outside the refund-approval workflow profile",
    "action_mismatch": "verified action differs from expected_action",
    "malformed_input": "request is not a well-formed verification request",
    "sdk_pin_mismatch": "installed pic-standard commit differs from the pinned SDK commit",
    "clock_rejected": "supplied test clock is invalid or not permitted in this process",
    "verifier_error": "verification raised an unexpected error",
    "verification_incomplete": "all gates passed but not every check was established",
}
AMBIGUOUS_ID_TEXT = "canonical evidence id is not unique; stage attribution withheld"


def installed_sdk_commit() -> str | None:
    """Commit recorded by pip in the installed pic-standard direct_url.json."""
    try:
        du = importlib.metadata.distribution("pic-standard").read_text("direct_url.json")
        return (json.loads(du).get("vcs_info") or {}).get("commit_id") if du else None
    except Exception:
        return None


def _checks(status: str = "not_evaluated") -> dict:
    return dict.fromkeys(CHECKS, status)


def _response(decision: str, checks: dict, reason: str | None, installed: str | None, *,
              claims: dict | None = None, action: dict | None = None,
              detail: list | None = None) -> dict:
    allowed = decision == "allow"
    trace = ([REASON_TEXT[reason]] if reason else []) + list(detail or [])
    return {
        "adapter_contract": CONTRACT,
        "decision": decision,
        "checks": checks,
        "verified_claims": copy.deepcopy(claims) if allowed else None,
        "verified_action": copy.deepcopy(action) if allowed else None,
        "reason_code": reason,
        "trace": trace,
        "evidence": {"example_commit": EXAMPLE_COMMIT, "pic_sdk_commit": SDK_COMMIT,
                     "installed_pic_sdk_commit": installed},
    }


def _validate_request(request: Any) -> str | None:
    """Return a fixed description of the first shape problem, or None."""
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        return f"request must have exactly the keys {sorted(REQUEST_KEYS)}"
    if not isinstance(request["proposal"], dict):
        return "proposal must be an object"
    expected = request["expected_action"]
    if not isinstance(expected, dict) or set(expected) != ACTION_KEYS:
        return f"expected_action must have exactly the keys {sorted(ACTION_KEYS)}"
    if not isinstance(expected["tool"], str) or not isinstance(expected["impact"], str):
        return "expected_action tool and impact must be strings"
    ctx = request["verification_context"]
    if not isinstance(ctx, dict) or not CONTEXT_REQUIRED <= set(ctx) <= CONTEXT_ALLOWED:
        return (f"verification_context must have {sorted(CONTEXT_REQUIRED)} and may "
                "only add 'clock'")
    if ctx["profile"] != PROFILE:
        return f"profile must be {PROFILE!r}"
    if not isinstance(ctx["keyring_fixture"], str) or ctx["keyring_fixture"] not in KEYRINGS:
        return f"keyring_fixture must be one of {sorted(KEYRINGS)}"
    return None


def _load_example():
    if str(VENDOR_DIR) not in sys.path:
        raise RuntimeError("vendored example is not importable in this process")
    import integration
    import sign_and_verify
    for module in (integration, sign_and_verify):
        if Path(module.__file__).resolve().parent != VENDOR_DIR:
            raise RuntimeError("vendored module resolved outside the vendored example")
    return integration, sign_and_verify


def _canonical_entry(integration, proposal: dict):
    """(evidence entry, parsed attestation) for the canonical signature, or (None, None)."""
    try:
        entry, attestation = integration._parse_canonical_sig_evidence(proposal)
    except (AttributeError, TypeError):
        return None, None
    return (entry, attestation) if isinstance(entry, dict) else (None, None)


def _entry_id_is_unique(proposal: dict, entry_id: Any) -> bool:
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list):
        return False
    return sum(1 for ev in evidence if isinstance(ev, dict) and ev.get("id") == entry_id) == 1


def _failed_messages(error, entry_id: Any) -> list[str]:
    """SDK messages for one evidence id. Internal use only; never returned."""
    details = getattr(error, "details", None)
    failed = details.get("failed") if isinstance(details, dict) else None
    if not isinstance(failed, list):
        return []
    return [str(item.get("message", "")) for item in failed
            if isinstance(item, dict) and item.get("id") == entry_id]


def verify_approval(request: Any) -> dict:
    """Verify one approval request. Never dispatches.

    Ordinary exceptions become decision "error"; process-control exceptions
    such as KeyboardInterrupt propagate.
    """
    installed = installed_sdk_commit()
    try:
        problem = _validate_request(request)
        if problem:
            return _response("error", _checks(), "malformed_input", installed, detail=[problem])
        if installed != SDK_COMMIT:
            return _response("error", _checks(), "sdk_pin_mismatch", installed)
        ctx = request["verification_context"]
        clock = (fixed_utc_now(parse_test_clock(ctx["clock"])) if "clock" in ctx
                 else contextlib.nullcontext())
        with clock:
            return _verify(request, installed)
    except ClockError:
        return _response("error", _checks(), "clock_rejected", installed)
    except Exception as e:
        return _response("error", _checks(), "verifier_error", installed,
                         detail=[f"exception type {type(e).__name__}"])


def _verify(request: dict, installed: str) -> dict:
    integration, sign_and_verify = _load_example()
    from pic_standard.canonical import canonicalize
    from pic_standard.errors import PICErrorCode
    from pic_standard.keyring import TrustedKeyRing
    from pic_standard.pipeline import verify_proposal

    proposal = copy.deepcopy(request["proposal"])
    expected = request["expected_action"]
    keyring_path = KEYRINGS[request["verification_context"]["keyring_fixture"]]
    checks = _checks()

    if integration._validate_refund_args(expected["args"]):
        return _response("error", checks, "malformed_input", installed,
                         detail=["expected_action.args outside the refund profile schema"])

    # Independent observation: configured status of the canonical entry's key_id.
    entry, attestation = _canonical_entry(integration, proposal)
    key_status = None
    if entry is not None:
        key_status = TrustedKeyRing.from_json_file(keyring_path).key_status(entry.get("key_id"))
        checks["signer_trust"] = "pass" if key_status == "ok" else "fail"

    result = verify_proposal(
        proposal, options=sign_and_verify.make_pipeline_options(keyring_path=keyring_path))

    if not result.ok:
        code = result.error.code if result.error else None
        detail = [f"pic_error_code={code.value if code else None}"]
        for stage in ("signature", "proposal_binding", "freshness"):
            checks[stage] = "unavailable"
        reason = None
        if entry is not None and _entry_id_is_unique(proposal, entry.get("id")):
            for message in _failed_messages(result.error, entry.get("id")):
                for pattern, stage, why in DIAGNOSTICS:
                    if pattern.fullmatch(message):
                        checks[stage] = "fail"
                        reason = reason or why
        elif entry is not None:
            detail.append(AMBIGUOUS_ID_TEXT)
        reason = KEY_STATUS_REASONS.get(key_status, reason)
        if code == PICErrorCode.INTERNAL_ERROR:
            return _response("error", checks, "verifier_error", installed, detail=detail)
        if reason is None:
            reason = ("evidence_rejected_unclassified" if code == PICErrorCode.EVIDENCE_FAILED
                      else "proposal_rejected")
        return _response("deny", checks, reason, installed, detail=detail)

    # Overall success at this pin means every evidence result was ok, so the
    # canonical entry completed signature, binding and applicable freshness.
    verified_ids = getattr(result.evidence_report, "signature_verified_ids", set()) or set()
    entry_verified = entry is not None and entry.get("id") in verified_ids
    checks["signature"] = checks["proposal_binding"] = (
        "pass" if entry_verified else "unavailable")
    has_expiry = (attestation is not None
                  and integration._validate_attestation_expiry(attestation) is None)
    # The SDK enforces expires_at only when present; freshness passes only when
    # the canonical entry verified AND the profile-required expiry was present.
    checks["freshness"] = ("pass" if entry_verified and has_expiry
                           else "not_evaluated" if entry_verified else "unavailable")

    # Workflow profile, verify_and_dispatch steps 2 to 6 in order. Helper
    # messages may contain input values, so only fixed text is returned.
    args = proposal.get("action", {}).get("args")
    steps = (
        (lambda: attestation is None, "canonical PIC-ATT/1.0 attestation required"),
        (lambda: integration._validate_action_tool_impact(proposal),
         "action tool or impact outside the refund profile"),
        (lambda: integration._validate_attestation_expiry(attestation),
         "canonical attestation lacks a usable expires_at"),
        (lambda: integration._validate_refund_args(args),
         "refund arguments outside the profile schema"),
    )
    failed_step = next((text for check, text in steps if check()), None)
    claim = None
    if failed_step is None:
        claim, claim_err = integration._parse_and_validate_merchant_claim(proposal)
        if claim_err or claim is None:
            failed_step = "merchant claim outside the profile schema"
    checks["workflow_profile"] = "fail" if failed_step else "pass"
    if failed_step:
        return _response("deny", checks, "profile_rejected", installed, detail=[failed_step])

    verified_action = {"tool": proposal["action"]["tool"], "impact": proposal["impact"],
                       "args": proposal["action"]["args"]}
    expected_action = {key: expected[key] for key in ACTION_KEYS}
    same = canonicalize(verified_action) == canonicalize(expected_action)
    checks["expected_action"] = "pass" if same else "fail"
    if not same:
        return _response("deny", checks, "action_mismatch", installed)

    if all(checks[name] == "pass" for name in CHECKS):
        return _response("allow", checks, None, installed, claims=claim, action=verified_action)
    return _response("deny", checks, "verification_incomplete", installed)
