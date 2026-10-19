"""Boundary tests for the synthetic policy; not PIC verification/conformance."""
import unittest
from recovery import Context, Engine, Record, reported_outcome


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


if __name__ == "__main__":
    unittest.main()
