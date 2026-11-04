"""Candidate retention policy for the synthetic, single-threaded fixture.

Receipt retention and admission retention are independent. After receipt pruning,
a scoped tombstone blocks dispatch. After any tombstone expires, admission of ALL
unrecognized operations closes: absent an external admission authority this
fixture cannot distinguish a forgotten operation from a genuinely new one.
The global closure flag survives pruning; this is not total loss of all state.
No persistence, restart, concurrency or production retention policy is claimed.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from recovery import Engine, Record, same_fixture_value, verified_decision


@dataclass
class Clock:
    now: int = 0

    def advance(self, seconds: int) -> None:
        if type(seconds) is not int or seconds < 0:
            raise ValueError("clock advances by nonnegative integer seconds")
        self.now += seconds


class RetentionEngine(Engine):
    def __init__(self, *, clock=None, receipt_ttl=86400, admission_ttl=604800,
                 **kwargs):
        super().__init__(**kwargs)
        if self.unguarded or self.unsafe_error_eviction:
            raise ValueError("retention policy excludes unsafe control modes")
        if not (type(receipt_ttl) is int and type(admission_ttl) is int
                and 0 < receipt_ttl < admission_ttl):
            raise ValueError("require 0 < receipt_ttl < admission_ttl")
        self.clock = clock if clock is not None else Clock()
        self.receipt_ttl, self.admission_ttl = receipt_ttl, admission_ttl
        self.admitted_at: dict[tuple, int] = {}
        self.tombstones: dict[tuple, Record] = {}
        self.admission_closed = False

    def prune(self) -> None:
        # Synchronous with call(), with no await between prune/lookup/reservation.
        # This ordering is only meaningful inside this single-threaded fixture.
        for scope, admitted in list(self.admitted_at.items()):
            age = self.clock.now - admitted
            if age >= self.admission_ttl:
                self.records.pop(scope, None)
                self.tombstones.pop(scope, None)
                del self.admitted_at[scope]
                self.decisions = {k: op for k, op in self.decisions.items()
                                  if not (k[:3] == scope[:3] and op == scope[3])}
                self.admission_closed = True
                self.events.append({"event": "admission_evidence_expired",
                                    "operation_id": scope[3], "at": self.clock.now,
                                    "new_admission": "closed"})
            elif age >= self.receipt_ttl and scope in self.records:
                old = self.records.pop(scope)
                self.tombstones[scope] = Record(copy.deepcopy(old.args),
                                                decision_id=old.decision_id)
                self.events.append({"event": "receipt_pruned_tombstone_retained",
                                    "operation_id": scope[3], "at": self.clock.now})

    def snapshot(self) -> dict:
        return {"clock": self.clock.now, "receipt_records": len(self.records),
                "tombstones": len(self.tombstones),
                "decision_bindings": len(self.decisions),
                "admission_closed": self.admission_closed,
                "provider_effect_count": len(self.provider.effects)}

    def call(self, operation_id: str, payment_id: str, amount_minor: int,
             currency: str, claimed_created_at: int | None = None) -> dict:
        # claimed_created_at is intentionally never used as admission authority.
        # The server-owned admission time cannot be refreshed by a caller.
        self.prune()
        c = self.context
        scope = (c.tenant, c.principal, "refund", operation_id)
        args = {"payment_id": payment_id, "amount_minor": amount_minor,
                "currency": currency}
        if (not isinstance(operation_id, str) or not operation_id
                or not isinstance(payment_id, str) or not payment_id
                or type(amount_minor) is not int or amount_minor <= 0
                or currency != "EUR"):
            return {"status": "invalid_request"}
        if any(k[3] == operation_id and k[:3] != scope[:3]
               for k in (*self.records, *self.tombstones)):
            return {"status": "denied"}
        if scope in self.tombstones:
            if not c.disclose:
                return {"status": "denied"}
            old = self.tombstones[scope]
            if not same_fixture_value(old.args, args):
                return {"status": "conflict"}
            approved = verified_decision(c.approval, args) if self.require_approval else None
            if approved is not None and approved != old.decision_id:
                return {"status": "conflict"}
            self.events.append({"event": "redispatch_refused_tombstone",
                                "operation_id": operation_id})
            return {"status": "unknown", "reason": "receipt_pruned"}
        if scope not in self.records and self.admission_closed:
            self.events.append({"event": "admission_refused_after_expiry",
                                "operation_id": operation_id})
            return {"status": "unknown", "reason": "admission_closed"}
        outcome = super().call(operation_id, payment_id, amount_minor, currency)
        if scope in self.records and scope not in self.admitted_at:
            self.admitted_at[scope] = self.clock.now
        return outcome
