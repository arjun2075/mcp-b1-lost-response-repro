"""Synthetic, in-process application recovery policy; NOT PIC or MCP semantics.

Single-threaded reservation only: no durability, replicas or crash safety claimed.
The provider oracle is deliberately separate from the recovery records. Only the
harness may configure authentication/policy/faults; these are not tool arguments.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Context:
    tenant: str = "tenant-a"
    principal: str = "principal-a"
    execute: bool = True
    disclose: bool = True


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


class Engine:
    def __init__(self, *, unguarded=False, post_commit_error=False,
                 unsafe_error_eviction=False):
        self.provider = Provider()
        self.records: dict[tuple, Record] = {}
        self.events: list[dict] = []
        self.context = Context()
        self.unguarded = unguarded
        self.post_commit_error = post_commit_error
        self.unsafe_error_eviction = unsafe_error_eviction
        self.fault_used = False

    def call(self, operation_id: str, payment_id: str,
             amount_minor: int, currency: str) -> dict:
        c = self.context  # set by harness, not supplied by caller
        # Reject bool (an int subclass), nonintegers and malformed refund shapes.
        if (not operation_id or not payment_id or type(amount_minor) is not int
                or amount_minor <= 0 or currency != "EUR"):
            return {"status": "invalid_request"}
        args = {"payment_id": payment_id, "amount_minor": amount_minor,
                "currency": currency}
        scope = (c.tenant, c.principal, "refund", operation_id)
        record = None if self.unguarded else self.records.get(scope)
        # A different identity cannot recover this fixture's original operation.
        # A separately admitted operation in another scope is outside this case.
        foreign = any(k[3] == operation_id and k[:3] != scope[:3]
                      for k in self.records)
        if foreign:
            return {"status": "denied"}  # no existence/detail/receipt disclosure
        if record is not None:
            if record.args != args:
                return {"status": "conflict"}
            if not c.disclose:
                return {"status": "denied"}
            if record.receipt is None:
                return {"status": "unknown"}
            return {"status": "completed", "receipt": copy.deepcopy(record.receipt)}
        if not c.execute:
            return {"status": "denied"}
        record = Record(copy.deepcopy(args))
        if not self.unguarded:
            self.records[scope] = record
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
