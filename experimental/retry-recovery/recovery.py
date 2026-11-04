"""Synthetic, in-process application recovery policy; NOT PIC or MCP semantics.

Single-threaded reservation only: no durability, replicas or crash safety claimed.
The provider oracle is deliberately separate from the recovery records. Only the
harness may configure authentication/policy/faults; these are not tool arguments.

Input validation is tightened relative to the first harness revision:
operation_id and payment_id must be non-empty strings (previously only truthy)
before any lookup or reservation. The valid baseline scenarios are unaffected.

Optional signed-approval mode (Engine(require_approval=True)) reads an adapter
response already produced by pic_adapter (contract retry-recovery-draft-1) and
placed in the harness-owned Context. This module never verifies approvals and
never calls the verifier. The decision-to-operation association below is a rule
of this signed-refund fixture. It does not prescribe MCP operation identity,
require PIC approval fields in an SEP, or constrain how other profiles
authorize operations.

Retained recovery currently ignores approval freshness and signer status and
uses independent disclosure permission. Adoption of this behavior for the
revoked-signer case (A03) remains subject to review.

Cross-context checks reject an attempted context change without returning the
original receipt. They are fixture rules, not existence hiding: a request may
behave differently depending on whether an identifier exists in another
context, and indistinguishability is neither claimed nor tested.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Context:
    tenant: str = "tenant-a"
    principal: str = "principal-a"
    execute: bool = True
    disclose: bool = True
    # Adapter response for the approval presented with this attempt, if any.
    approval: dict | None = None


@dataclass
class Provider:
    effects: list[dict[str, Any]] = field(default_factory=list)

    def commit(self, operation: str, args: dict) -> dict:
        receipt = {"receipt_marker": f"fixture-receipt-{len(self.effects) + 1}",
                   "operation_id": operation, "args": copy.deepcopy(args)}
        self.effects.append(copy.deepcopy(receipt))
        return receipt


@dataclass
class Record:
    args: dict
    receipt: dict | None = None
    decision_id: str | None = None  # verified decision bound at reservation


def _check_fixture_value(value: Any) -> None:
    """Allow only the value types this fixture compares: str, non-bool int, str-keyed dict."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("fixture comparison supports string keys only")
            _check_fixture_value(item)
    elif isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"fixture comparison does not support {type(value).__name__}")


def same_fixture_value(a: Any, b: Any) -> bool:
    """Fixture-specific, type-strict comparison of restricted refund values.

    Deterministic json.dumps serialization after type validation; this is not
    PIC-CJSON or RFC 8785 canonicalization. Unsupported types raise ValueError.
    """
    for value in (a, b):
        _check_fixture_value(value)
    dump = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"))  # noqa: E731
    return dump(a) == dump(b)


def verified_decision(approval: Any, args: dict) -> str | None:
    """decision_id of an allowed approval whose verified refund action equals args.

    Any malformed or non-allow response yields None. This reads the adapter's
    result; it does not re-verify the approval.
    """
    if not isinstance(approval, dict) or approval.get("decision") != "allow":
        return None
    action, claims = approval.get("verified_action"), approval.get("verified_claims")
    if not isinstance(action, dict) or not isinstance(claims, dict):
        return None
    try:
        if not same_fixture_value(action, {"tool": "refund", "impact": "money", "args": args}):
            return None
    except ValueError:
        return None
    decision_id = claims.get("decision_id")
    return decision_id if isinstance(decision_id, str) and decision_id else None


class Engine:
    def __init__(self, *, unguarded=False, post_commit_error=False,
                 unsafe_error_eviction=False, require_approval=False):
        if require_approval and (unguarded or unsafe_error_eviction):
            raise ValueError("require_approval is not supported with the unguarded "
                             "or unsafe-eviction control modes")
        self.provider = Provider()
        self.records: dict[tuple, Record] = {}
        # (tenant, principal, tool, verified decision_id) -> operation_id
        self.decisions: dict[tuple, str] = {}
        self.events: list[dict] = []
        self.context = Context()
        self.unguarded = unguarded
        self.post_commit_error = post_commit_error
        self.unsafe_error_eviction = unsafe_error_eviction
        self.require_approval = require_approval
        self.fault_used = False

    def call(self, operation_id: str, payment_id: str,
             amount_minor: int, currency: str) -> dict:
        c = self.context  # set by harness, not supplied by caller
        # IDs must be non-empty strings; reject bool (an int subclass), nonintegers
        # and malformed refund shapes, all before any lookup or reservation.
        if (not isinstance(operation_id, str) or not operation_id
                or not isinstance(payment_id, str) or not payment_id
                or type(amount_minor) is not int
                or amount_minor <= 0 or currency != "EUR"):
            return {"status": "invalid_request"}
        args = {"payment_id": payment_id, "amount_minor": amount_minor,
                "currency": currency}
        scope = (c.tenant, c.principal, "refund", operation_id)
        record = None if self.unguarded else self.records.get(scope)
        # Fixture rule: a different authenticated context cannot recover this
        # fixture's original operation; the receipt is not returned. This is not
        # existence hiding (see module docstring).
        foreign = any(k[3] == operation_id and k[:3] != scope[:3]
                      for k in self.records)
        if foreign:
            return {"status": "denied"}
        approved = verified_decision(c.approval, args) if self.require_approval else None
        if record is not None:
            # In approval mode, denied disclosure is answered before any recovery
            # distinction (conflict, decision mismatch, unknown) is examined.
            if self.require_approval and not c.disclose:
                return {"status": "denied"}
            if not same_fixture_value(record.args, args):
                return {"status": "conflict"}
            # Ordinary retained recovery needs no fresh approval. A currently
            # verified approval presented here must belong to the retained
            # decision; an unverified one (expired, revoked) is not consulted.
            if approved is not None and approved != record.decision_id:
                self.events.append({"event": "approval_decision_mismatch",
                                    "operation_id": operation_id})
                return {"status": "conflict"}
            if not c.disclose:
                return {"status": "denied"}
            if record.receipt is None:
                return {"status": "unknown"}
            return {"status": "completed", "receipt": copy.deepcopy(record.receipt)}
        decision_scope = None
        if self.require_approval:
            if approved is None:
                reason = c.approval.get("reason_code") if isinstance(c.approval, dict) else None
                self.events.append({"event": "approval_not_verified",
                                    "operation_id": operation_id, "reason_code": reason})
                return {"status": "denied"}
            decision_scope = (c.tenant, c.principal, "refund", approved)
            # Proposed for review: a verified decision already bound in another
            # context is rejected here. Scoped identifiers are not automatically
            # globally unique or globally forbidden; this is a fixture policy choice.
            if any(k[3] == approved and k[:3] != decision_scope[:3] for k in self.decisions):
                self.events.append({"event": "decision_bound_in_other_context",
                                    "operation_id": operation_id})
                return {"status": "denied"}
            if decision_scope in self.decisions:
                # Same verified decision under a fresh operation id: never a new effect.
                # Conflict versus returning the original outcome is open for review.
                self.events.append({"event": "decision_already_bound",
                                    "operation_id": operation_id})
                return {"status": "conflict"}
        if not c.execute:
            return {"status": "denied"}
        record = Record(copy.deepcopy(args), decision_id=approved)
        if not self.unguarded:
            self.records[scope] = record
            if decision_scope is not None:
                # Bound at reservation, before dispatch; survives errors/unknown outcome.
                self.decisions[decision_scope] = operation_id
        self.events.append({"event": "reserved", "operation_id": operation_id})
        receipt = self.provider.commit(operation_id, args)
        self.events.append({"event": "effect_committed", "operation_id": operation_id})
        if self.post_commit_error and not self.fault_used:
            self.fault_used = True
            # Provider acted, recovery knows only reservation; NOT non-execution.
            if self.unsafe_error_eviction:
                self.records.pop(scope, None)
            self.events.append({"event": "post_commit_error", "operation_id": operation_id})
            return {"status": "execution_error", "outcome": "unknown"}
        record.receipt = receipt
        if not c.disclose:
            return {"status": "denied"}
        return {"status": "completed", "receipt": copy.deepcopy(receipt)}


def reported_outcome(observation: dict) -> str:
    """Scripted client interpretation, not a model-behavior experiment."""
    if observation["kind"] != "response":
        return "unknown"
    p = observation["payload"]
    if p.get("status") == "completed" and isinstance(p.get("receipt"), dict):
        return "confirmed"
    # Denial/conflict/error says nothing about prior provider completion.
    return "unknown"
