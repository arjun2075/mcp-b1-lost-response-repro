"""C01 synthetic concurrent retry fixture; not MCP or production semantics.

Two variants share an independently observed provider-effect ledger:
* reserved: protect check+reservation with a lock before any provider await;
* unsafe: check before awaiting a provider gate, then record afterwards.

The externally held gate makes real overlapping requests deterministic without
sleeps. The reserved variant does not hold the lock across provider execution.
No durability, restart, multi-process, real payments, or cryptographic approval.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import anyio


@dataclass(frozen=True)
class Identity:
    tenant: str = "tenant-a"
    principal: str = "principal-a"
    execute: bool = True
    disclose: bool = True


@dataclass
class Record:
    args: dict
    receipt: dict | None = None


@dataclass
class GatedProvider:
    effects: list[dict] = field(default_factory=list)
    attempts: int = 0
    first_entered: anyio.Event = field(default_factory=anyio.Event)
    second_entered: anyio.Event = field(default_factory=anyio.Event)
    release: anyio.Event = field(default_factory=anyio.Event)

    async def commit(self, operation_id: str, args: dict) -> dict:
        self.attempts += 1
        if self.attempts == 1:
            self.first_entered.set()
        if self.attempts == 2:
            self.second_entered.set()
        await self.release.wait()
        receipt = {"receipt_marker": f"fixture-receipt-{len(self.effects) + 1}",
                   "operation_id": operation_id, "args": copy.deepcopy(args)}
        self.effects.append(copy.deepcopy(receipt))
        return receipt


class ConcurrentEngine:
    def __init__(self, *, unsafe=False, post_commit_error=False):
        self.unsafe = unsafe
        self.post_commit_error = post_commit_error
        self.provider = GatedProvider()
        self.records: dict[tuple, Record] = {}
        self._lock = anyio.Lock()
        self.fault_used = False
        self.events: list[dict] = []

    def _existing(self, scope: tuple, args: dict, identity: Identity) -> dict | None:
        if any(k[3] == scope[3] and k[:3] != scope[:3] for k in self.records):
            return {"status": "denied"}
        old = self.records.get(scope)
        if old is None:
            return None
        if not identity.disclose:
            return {"status": "denied"}
        if old.args != args:
            return {"status": "conflict"}
        if old.receipt is None:
            return {"status": "unknown", "reason": "in_progress_or_indeterminate"}
        return {"status": "completed", "receipt": copy.deepcopy(old.receipt)}

    async def call(self, operation_id: str, payment_id: str, amount_minor: int,
                   currency: str, *, identity: Identity | None = None) -> dict:
        identity = identity if identity is not None else Identity()
        if (not isinstance(identity, Identity)
                or not isinstance(operation_id, str) or not operation_id
                or not isinstance(payment_id, str) or not payment_id
                or type(amount_minor) is not int or amount_minor <= 0
                or currency != "EUR"):
            return {"status": "invalid_request"}
        args = {"payment_id": payment_id, "amount_minor": amount_minor,
                "currency": currency}
        scope = (identity.tenant, identity.principal, "refund", operation_id)
        if self.unsafe:
            # Deliberate check-then-act bug: an await can separate the empty
            # lookup from the new record. Only use for negative controls.
            found = self._existing(scope, args, identity)
            if found is not None:
                return found
            if not identity.execute:
                return {"status": "denied"}
            self.events.append({"kind": "unsafe_check_without_reservation", "op": operation_id})
            receipt = await self.provider.commit(operation_id, args)
            record = Record(copy.deepcopy(args))
            self.records[scope] = record
        else:
            # Critical section includes check and reservation, but never an
            # external effect or remote await. Second caller sees pending record.
            async with self._lock:
                found = self._existing(scope, args, identity)
                if found is not None:
                    return found
                if not identity.execute:
                    return {"status": "denied"}
                record = Record(copy.deepcopy(args))
                self.records[scope] = record
                self.events.append({"kind": "reserved", "op": operation_id})
            receipt = await self.provider.commit(operation_id, args)
        self.events.append({"kind": "effect_committed", "op": operation_id})
        if self.post_commit_error and not self.fault_used:
            self.fault_used = True
            self.events.append({"kind": "post_commit_error", "op": operation_id})
            return {"status": "execution_error", "outcome": "unknown"}
        record.receipt = copy.deepcopy(receipt)
        if not identity.disclose:
            return {"status": "denied"}
        return {"status": "completed", "receipt": copy.deepcopy(receipt)}
