"""Boundary tests for the synthetic policy; not PIC verification/conformance."""
import unittest
from recovery import (Context, Engine, Record, reported_outcome, same_fixture_value,
                      verified_decision)


class RecoveryTests(unittest.TestCase):
    def call(self, engine, operation="op", amount=100):
        return engine.call(operation, "payment", amount, "EUR")

    def test_unknown_reservation_does_not_execute(self):
        e = Engine()
        e.records[("tenant-a", "principal-a", "refund", "op")] = Record(
            {"payment_id": "payment", "amount_minor": 100, "currency": "EUR"})
        self.assertEqual(self.call(e), {"status": "unknown"})
        self.assertEqual(len(e.provider.effects), 0)

    def test_replayed_receipt_cannot_mutate_provider_or_store(self):
        e = Engine()
        self.call(e)
        replay = self.call(e)
        replay["receipt"]["args"]["amount_minor"] = 999
        self.assertEqual(self.call(e)["receipt"]["args"]["amount_minor"], 100)
        self.assertEqual(e.provider.effects[0]["args"]["amount_minor"], 100)

    def test_revoke_new_execution_still_recognizes_prior_operation(self):
        e = Engine()
        original = self.call(e)
        e.context = Context(execute=False)
        self.assertEqual(self.call(e), original)
        self.assertEqual(self.call(e, "new"), {"status": "denied"})
        self.assertEqual(len(e.provider.effects), 1)

    def test_principal_isolation(self):
        e = Engine()
        self.call(e)
        e.context = Context(principal="other")
        self.assertEqual(self.call(e), {"status": "denied"})
        self.assertEqual(len(e.provider.effects), 1)

    def test_invalid_amounts_never_dispatch(self):
        for amount in [True, 1.5, 0, -1]:
            e = Engine()
            self.assertEqual(self.call(e, amount=amount), {"status": "invalid_request"})
            self.assertEqual(len(e.provider.effects), 0)

    def test_error_after_commit_is_unknown_and_does_not_retry(self):
        e = Engine(post_commit_error=True)
        first = self.call(e)
        self.assertEqual(first["outcome"], "unknown")
        self.assertEqual(self.call(e), {"status": "unknown"})
        self.assertEqual(len(e.provider.effects), 1)

    def test_negative_readback_cannot_resolve_inflight_operation(self):
        e = Engine()
        args = {"payment_id": "payment", "amount_minor": 100, "currency": "EUR"}
        e.records[("tenant-a", "principal-a", "refund", "op")] = Record(args)
        # Barrier-controlled state: reserved/in flight, provider has no visible effect yet.
        self.assertEqual(e.provider.effects, [])
        self.assertEqual(self.call(e), {"status": "unknown"})
        # Original in-flight work now commits; no retry was dispatched.
        e.provider.commit("op", args)
        self.assertEqual(self.call(e), {"status": "unknown"})
        self.assertEqual(len(e.provider.effects), 1)

    def test_empty_success_is_not_confirmation(self):
        self.assertEqual(reported_outcome({"kind": "response", "payload": {}}), "unknown")


ARGS = {"payment_id": "payment", "amount_minor": 100, "currency": "EUR"}
SCOPE = ("tenant-a", "principal-a", "refund", "op")


def allowed(decision_id="decision-001", args=None):
    """Synthetic adapter 'allow' response; not produced by PIC verification."""
    return {"decision": "allow", "reason_code": None,
            "verified_claims": {"decision_id": decision_id, "approval_ref": "approval-001"},
            "verified_action": {"tool": "refund", "impact": "money",
                                "args": dict(ARGS if args is None else args)}}


def rejected(reason="approval_expired"):
    """Synthetic adapter 'deny' response; not produced by PIC verification."""
    return {"decision": "deny", "reason_code": reason,
            "verified_claims": None, "verified_action": None}


class ApprovalModeTests(unittest.TestCase):
    """Engine policy in approval mode, driven by SYNTHETIC adapter responses.

    These establish how the engine consumes an adapter result. They do not
    exercise PIC verification; the MCP scenarios in run_cases.py integrate the
    engine with real pinned PIC verification through pic_adapter.
    """

    def engine(self, approval=None, **kwargs):
        e = Engine(require_approval=True, **kwargs)
        e.context = Context(approval=allowed() if approval is None else approval)
        return e

    def call(self, engine, operation="op", amount=100, payment="payment"):
        return engine.call(operation, payment, amount, "EUR")

    def test_invalid_ids_rejected_before_any_record_or_effect(self):
        for operation, payment in [(5, "payment"), (["x"], "payment"), (None, "payment"),
                                   ("", "payment"), ("op", 5), ("op", {"a": 1}), ("op", "")]:
            for require_approval in (False, True):
                with self.subTest(operation=operation, payment=payment,
                                  require_approval=require_approval):
                    e = Engine(require_approval=require_approval)
                    e.context = Context(approval=allowed())
                    self.assertEqual(e.call(operation, payment, 100, "EUR"),
                                     {"status": "invalid_request"})
                    self.assertEqual((e.provider.effects, e.records, e.decisions), ([], {}, {}))

    def test_verified_approval_executes_once_and_binds_decision(self):
        e = self.engine()
        first = self.call(e)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(self.call(e), first)
        self.assertEqual(len(e.provider.effects), 1)
        self.assertEqual(e.records[SCOPE].decision_id, "decision-001")
        self.assertEqual(e.decisions, {("tenant-a", "principal-a", "refund", "decision-001"): "op"})

    def test_unverified_approval_cannot_create_an_effect(self):
        for approval in (rejected(), rejected("signer_revoked"), {"decision": "error"},
                         None, "allow", {"decision": "allow"}):
            with self.subTest(approval=approval):
                e = Engine(require_approval=True)
                e.context = Context(approval=approval)
                self.assertEqual(self.call(e), {"status": "denied"})
                self.assertEqual((e.provider.effects, e.records, e.decisions), ([], {}, {}))

    def test_approved_action_must_match_requested_action_type_strictly(self):
        for args in ({**ARGS, "amount_minor": 999}, {**ARGS, "amount_minor": True},
                     {**ARGS, "amount_minor": 100.0}, {**ARGS, "extra": "x"}):
            with self.subTest(args=args):
                e = self.engine(allowed(args=args))
                self.assertEqual(self.call(e), {"status": "denied"})
                self.assertEqual(len(e.provider.effects), 0)

    def test_malformed_synthetic_approval_is_not_verified_and_does_not_raise(self):
        bad_action = {"tool": "refund", "impact": "money", "args": {**ARGS, "x": 1.5}}
        for approval in ({"decision": "allow", "verified_action": bad_action,
                          "verified_claims": {"decision_id": "d"}},
                         {"decision": "allow", "verified_action": [],
                          "verified_claims": {"decision_id": "d"}},
                         {**allowed(), "verified_claims": {"decision_id": ""}},
                         {**allowed(), "verified_claims": {"decision_id": 7}}):
            with self.subTest(approval=approval):
                self.assertIsNone(verified_decision(approval, ARGS))

    def test_execution_permission_still_required_with_valid_approval(self):
        e = Engine(require_approval=True)
        e.context = Context(execute=False, approval=allowed())
        self.assertEqual(self.call(e), {"status": "denied"})
        self.assertEqual((e.provider.effects, e.records, e.decisions), ([], {}, {}))
        # A04 in approval mode: execution withdrawn after a successful effect,
        # with a valid approval still presented.
        e = self.engine()
        receipt = self.call(e)["receipt"]
        e.context = Context(execute=False, approval=allowed())
        self.assertEqual(self.call(e), {"status": "completed", "receipt": receipt})
        e.context = Context(execute=False, disclose=False, approval=allowed())
        self.assertEqual(self.call(e), {"status": "denied"})
        records, decisions = dict(e.records), dict(e.decisions)
        e.context = Context(execute=False, approval=allowed("decision-002"))
        self.assertEqual(self.call(e, "op-2"), {"status": "denied"})
        self.assertEqual((e.records, e.decisions), (records, decisions))
        self.assertEqual(len(e.provider.effects), 1)

    def test_reservation_and_decision_survive_post_commit_error(self):
        e = self.engine(post_commit_error=True)
        self.assertEqual(self.call(e), {"status": "execution_error", "outcome": "unknown"})
        self.assertEqual(len(e.provider.effects), 1)
        self.assertIn(SCOPE, e.records)
        self.assertEqual(e.decisions, {("tenant-a", "principal-a", "refund", "decision-001"): "op"})
        self.assertEqual(self.call(e), {"status": "unknown"})
        self.assertEqual(self.call(e, "op-fresh"), {"status": "conflict"})
        self.assertEqual(len(e.provider.effects), 1)

    def test_denied_disclosure_masks_retained_conflicts(self):
        e = self.engine()
        self.call(e)
        for approval, amount in ((allowed(), 200), (allowed("decision-999"), 100),
                                 (rejected(), 200), (None, 100)):
            with self.subTest(approval=approval, amount=amount):
                e.context = Context(disclose=False, approval=approval)
                self.assertEqual(self.call(e, amount=amount), {"status": "denied"})
        e.context = Context(approval=allowed())
        self.assertEqual(self.call(e, amount=200), {"status": "conflict"})
        e.context = Context(approval=allowed("decision-999"))
        self.assertEqual(self.call(e), {"status": "conflict"})
        self.assertEqual(len(e.provider.effects), 1)

    def test_original_mode_keeps_conflict_before_disclosure(self):
        e = Engine()
        self.call(e)
        e.context = Context(disclose=False)
        self.assertEqual(self.call(e, amount=200), {"status": "conflict"})

    def test_same_decision_with_fresh_operation_id_adds_no_effect(self):
        # Provisional policy: conflict. Returning the original outcome is open for review.
        e = self.engine()
        self.call(e)
        for operation in ("op-2", "op-3"):
            self.assertEqual(self.call(e, operation), {"status": "conflict"})
        self.assertEqual(len(e.provider.effects), 1)
        self.assertEqual([ev["event"] for ev in e.events].count("decision_already_bound"), 2)

    def test_new_decision_with_identical_arguments_is_a_new_operation(self):
        e = self.engine()
        self.call(e)
        e.context = Context(approval=allowed("decision-002"))
        self.assertEqual(self.call(e, "op-2")["status"], "completed")
        self.assertEqual(len(e.provider.effects), 2)

    def test_retained_recovery_ignores_expired_or_revoked_approval(self):
        # Current behavior; adoption for A03 (revoked signer) remains subject to review.
        for reason in ("approval_expired", "signer_revoked"):
            with self.subTest(reason=reason):
                e = self.engine()
                receipt = self.call(e)["receipt"]
                e.context = Context(approval=rejected(reason))
                self.assertEqual(self.call(e), {"status": "completed", "receipt": receipt})
                e.context = Context(approval=rejected(reason), disclose=False)
                self.assertEqual(self.call(e), {"status": "denied"})
                e.context = Context(approval=rejected(reason))
                self.assertEqual(self.call(e, "op-new"), {"status": "denied"})
                self.assertEqual(len(e.provider.effects), 1)

    def test_reissued_approval_recovers_same_operation(self):
        e = self.engine()
        receipt = self.call(e)["receipt"]
        reissued = allowed()
        reissued["verified_claims"]["approval_ref"] = "approval-002"
        e.context = Context(approval=reissued)
        self.assertEqual(self.call(e), {"status": "completed", "receipt": receipt})
        self.assertEqual(len(e.provider.effects), 1)

    def test_other_context_cannot_recover_or_reuse_decision(self):
        e = self.engine()
        self.call(e)
        for context in (Context(tenant="tenant-b", approval=allowed()),
                        Context(principal="principal-b", approval=allowed())):
            with self.subTest(context=context):
                e.context = context
                self.assertEqual(self.call(e), {"status": "denied"})
                self.assertEqual(self.call(e, "op-b"), {"status": "denied"})
                self.assertNotIn("receipt", self.call(e))
        self.assertEqual(len(e.provider.effects), 1)

    def test_unsupported_mode_combinations_rejected(self):
        for kwargs in ({"unguarded": True}, {"unsafe_error_eviction": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Engine(require_approval=True, **kwargs)

    def test_fixture_comparison_is_type_strict(self):
        self.assertTrue(same_fixture_value(ARGS, dict(ARGS)))
        self.assertFalse(same_fixture_value({"a": 1}, {"a": 2}))
        with self.assertRaises(ValueError):
            same_fixture_value({"a": 1}, {"a": True})
        with self.assertRaises(ValueError):
            same_fixture_value({"a": 1.0}, {"a": 1})


if __name__ == "__main__":
    unittest.main()
