"""D01 durable-reservation restart fixture using two independent SQLite databases.

The application ledger and synthetic provider oracle persist in separate SQLite
files. Every invocation is a fresh process. A hard os._exit after provider commit
leaves the app reservation unresolved. Recovery never dispatches from that state;
a separately correlated, authoritative synthetic provider lookup can reconcile it.

No remote payment, real provider, actual authentication, distributed transaction,
replica safety, or PIC/MCP conformance is claimed.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

TOOL = "refund"


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def connect(path):
    con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    return con


def prepare(store, oracle):
    with closing(connect(store)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS operations (
            tenant TEXT NOT NULL, principal TEXT NOT NULL, tool TEXT NOT NULL,
            operation_id TEXT NOT NULL, args TEXT NOT NULL, state TEXT NOT NULL,
            receipt TEXT, PRIMARY KEY (tenant, principal, tool, operation_id)
        )""")
    with closing(connect(oracle)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS effects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant TEXT NOT NULL, principal TEXT NOT NULL, tool TEXT NOT NULL,
            operation_id TEXT NOT NULL, args TEXT NOT NULL, receipt TEXT NOT NULL
        )""")


def effect_count(oracle):
    with closing(connect(oracle)) as db:
        return db.execute("SELECT COUNT(*) FROM effects").fetchone()[0]


def lookup(db, scope):
    return db.execute("""SELECT args, state, receipt FROM operations
        WHERE tenant=? AND principal=? AND tool=? AND operation_id=?""", scope).fetchone()


def foreign(db, scope):
    return db.execute("""SELECT 1 FROM operations WHERE operation_id=?
        AND NOT (tenant=? AND principal=? AND tool=?) LIMIT 1""",
        (scope[3], scope[0], scope[1], scope[2])).fetchone() is not None


def retained(row, args, disclose):
    if not disclose:
        return {"status": "denied"}
    if row[0] != args:
        return {"status": "conflict"}
    if row[1] == "pending":
        return {"status": "unknown", "reason": "retained_indeterminate"}
    if row[1] == "completed" and row[2] is not None:
        return {"status": "completed", "receipt": json.loads(row[2])}
    raise AssertionError("invalid durable row state")


def commit_effect(oracle, scope, args):
    with closing(connect(oracle)) as db:
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute("""INSERT INTO effects
            (tenant, principal, tool, operation_id, args, receipt)
            VALUES (?, ?, ?, ?, ?, ?)""", (*scope, args, "{}"))
        receipt = {"provider_effect_id": cur.lastrowid, "operation_id": scope[3],
                   "args": json.loads(args)}
        db.execute("UPDATE effects SET receipt=? WHERE id=?", (canon(receipt), cur.lastrowid))
        db.execute("COMMIT")
        return receipt


def call(store, oracle, scope, args, *, strategy="reserved", crash=None,
         execute=True, disclose=True):
    with closing(connect(store)) as db:
        db.execute("BEGIN IMMEDIATE")
        if foreign(db, scope):
            db.execute("ROLLBACK")
            return {"status": "denied"}
        old = lookup(db, scope)
        if old is not None:
            db.execute("ROLLBACK")
            return retained(old, args, disclose)
        if not execute:
            db.execute("ROLLBACK")
            return {"status": "denied"}
        if strategy == "reserved":
            db.execute("""INSERT INTO operations
                (tenant, principal, tool, operation_id, args, state, receipt)
                VALUES (?, ?, ?, ?, ?, 'pending', NULL)""", (*scope, args))
        db.execute("COMMIT")
    if crash == "after_reservation":
        if strategy != "reserved":
            raise ValueError("after_reservation requires reserved mode")
        os._exit(36)  # deliberate hard process exit; no cleanup or handlers
    receipt = commit_effect(oracle, scope, args)
    if crash == "after_effect":
        os._exit(37)  # independent oracle commit durable; app receipt absent
    with closing(connect(store)) as db:
        db.execute("BEGIN IMMEDIATE")
        if strategy == "reserved":
            changed = db.execute("""UPDATE operations SET state='completed', receipt=?
                WHERE tenant=? AND principal=? AND tool=? AND operation_id=?
                AND args=? AND state='pending'""", (canon(receipt), *scope, args)).rowcount
            if changed != 1:
                db.execute("ROLLBACK")
                raise AssertionError("reservation changed during effect")
        else:
            # Deliberately unsafe: no reservation was stored before provider effect.
            db.execute("""INSERT OR REPLACE INTO operations
                (tenant, principal, tool, operation_id, args, state, receipt)
                VALUES (?, ?, ?, ?, ?, 'completed', ?)""", (*scope, args, canon(receipt)))
        db.execute("COMMIT")
    return {"status": "completed", "receipt": receipt} if disclose else {"status": "denied"}


def reconcile(store, oracle, scope, args, disclose=True):
    with closing(connect(store)) as db:
        db.execute("BEGIN IMMEDIATE")
        if foreign(db, scope):
            db.execute("ROLLBACK")
            return {"status": "denied"}
        old = lookup(db, scope)
        if old is None:
            db.execute("ROLLBACK")
            return {"status": "unknown", "reason": "no_retained_operation"}
        result = retained(old, args, disclose)
        if result["status"] != "unknown":
            db.execute("ROLLBACK")
            return result
        # Only exact scope/semantic match counts. Missing oracle data does not
        # prove the effect did not happen; multiple matches are ambiguous.
        with closing(connect(oracle)) as prov:
            matches = prov.execute("""SELECT receipt, args FROM effects WHERE
                tenant=? AND principal=? AND tool=? AND operation_id=?""", scope).fetchall()
        if len(matches) != 1 or matches[0][1] != args:
            db.execute("ROLLBACK")
            return {"status": "unknown", "reason": "lookup_absent_or_ambiguous"}
        receipt_text = matches[0][0]
        changed = db.execute("""UPDATE operations SET state='completed', receipt=? WHERE
            tenant=? AND principal=? AND tool=? AND operation_id=? AND state='pending'""",
            (receipt_text, *scope)).rowcount
        if changed != 1:
            db.execute("ROLLBACK")
            raise AssertionError("unexpected concurrent transition")
        db.execute("COMMIT")
        return {"status": "completed", "receipt": json.loads(receipt_text)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--action", choices=("call", "reconcile", "count"), default="call")
    parser.add_argument("--strategy", choices=("reserved", "unsafe"), default="reserved")
    parser.add_argument("--crash", choices=("after_reservation", "after_effect"))
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--principal", default="principal-a")
    parser.add_argument("--operation-id", default="operation-1")
    parser.add_argument("--payment-id", default="pay_A")
    parser.add_argument("--amount", type=int, default=4000)
    parser.add_argument("--currency", default="EUR")
    parser.add_argument("--deny-execute", action="store_true")
    parser.add_argument("--deny-disclose", action="store_true")
    p = parser.parse_args()
    prepare(p.store, p.oracle)
    if p.action == "count":
        print(canon({"effects": effect_count(p.oracle)}))
        return
    if not p.operation_id or not p.payment_id or p.amount <= 0 or p.currency != "EUR":
        print(canon({"status": "invalid_request"}))
        return
    scope = (p.tenant, p.principal, TOOL, p.operation_id)
    args = canon({"payment_id": p.payment_id, "amount_minor": p.amount, "currency": p.currency})
    if p.action == "reconcile":
        result = reconcile(p.store, p.oracle, scope, args, not p.deny_disclose)
    else:
        result = call(p.store, p.oracle, scope, args, strategy=p.strategy,
                      crash=p.crash, execute=not p.deny_execute,
                      disclose=not p.deny_disclose)
    print(canon(result))


if __name__ == "__main__":
    main()
