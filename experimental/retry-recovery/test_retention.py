"""Policy regressions; real PIC and MCP transitions live in run_retention_cases."""
import unittest

from recovery import Context
from retention import Clock, RetentionEngine

ARGS = {"operation_id": "op", "payment_id": "pay", "amount_minor": 100, "currency": "EUR"}


class RetentionTests(unittest.TestCase):
    def seeded(self, **kwargs):
        engine = RetentionEngine(receipt_ttl=10, admission_ttl=20, **kwargs)
        self.assertEqual(engine.call(**ARGS)["status"], "completed" if not kwargs else "execution_error")
        return engine

    def test_receipt_boundary_and_independent_oracle(self):
        e = self.seeded()
        original = e.provider.effects.copy()
        e.clock.advance(9)
        self.assertEqual(e.call(**ARGS)["receipt"], original[0])
        e.clock.advance(1)
        self.assertEqual(e.call(**ARGS), {"status": "unknown", "reason": "receipt_pruned"})
        self.assertEqual(e.records, {})
        self.assertEqual(len(e.tombstones), 1)
        self.assertIsNone(next(iter(e.tombstones.values())).receipt)
        self.assertEqual(e.provider.effects, original)

    def test_unresolved_reservation_prunes_without_redispatch(self):
        e = self.seeded(post_commit_error=True)
        e.clock.advance(10)
        self.assertEqual(e.call(**ARGS)["status"], "unknown")
        self.assertEqual(len(e.provider.effects), 1)

    def test_timestamp_cannot_refresh_admission(self):
        e = self.seeded()
        e.clock.advance(10)
        self.assertEqual(e.call(**ARGS, claimed_created_at=10)["status"], "unknown")
        self.assertEqual(list(e.admitted_at.values()), [0])
        e.clock.advance(10)
        self.assertEqual(e.call(**ARGS, claimed_created_at=20)["reason"], "admission_closed")
        self.assertEqual(len(e.provider.effects), 1)

    def test_global_refusal_after_tombstone_expiry(self):
        e = self.seeded()
        e.clock.advance(20)
        e.prune()
        self.assertEqual((e.records, e.tombstones, e.admitted_at, e.decisions), ({}, {}, {}, {}))
        self.assertTrue(e.admission_closed)
        for operation in ("op", "new-op"):
            self.assertEqual(e.call(**{**ARGS, "operation_id": operation})["reason"], "admission_closed")
        self.assertEqual(len(e.provider.effects), 1)

    def test_new_operation_allowed_while_tombstone_survives(self):
        e = self.seeded()
        e.clock.advance(10)
        self.assertEqual(e.call(**{**ARGS, "operation_id": "new"})["status"], "completed")
        self.assertEqual(len(e.provider.effects), 2)
        self.assertEqual(len(e.tombstones), 1)

    def test_pruned_context_and_disclosure_checks(self):
        e = self.seeded()
        e.clock.advance(10)
        for context in (Context(tenant="other"), Context(principal="other"), Context(disclose=False)):
            e.context = context
            response = e.call(**ARGS)
            self.assertEqual(response, {"status": "denied"})
            self.assertNotIn("fixture-receipt", str(response))
        self.assertEqual(len(e.provider.effects), 1)

    def test_tombstone_argument_mismatch_and_copy(self):
        e = self.seeded()
        e.clock.advance(10)
        self.assertEqual(e.call(**{**ARGS, "amount_minor": 200})["status"], "conflict")
        self.assertEqual(e.call(**ARGS)["status"], "unknown")
        self.assertEqual(len(e.provider.effects), 1)

    def test_clock_and_retention_configuration_reject_invalid_values(self):
        c = Clock()
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                c.advance(value)
        for receipt, admission in ((0, 10), (10, 10), (20, 10), (True, 20)):
            with self.assertRaises(ValueError):
                RetentionEngine(receipt_ttl=receipt, admission_ttl=admission)

    def test_expiry_does_not_erase_still_retained_result(self):
        e = self.seeded()
        e.clock.advance(15)
        self.assertEqual(e.call(**{**ARGS, "operation_id": "new"})["status"], "completed")
        e.clock.advance(5)
        # The old operation expires, closing new admissions. A newer retained
        # result can still be recovered without another effect.
        self.assertEqual(e.call(**{**ARGS, "operation_id": "new"})["status"], "completed")
        self.assertTrue(e.admission_closed)
        self.assertEqual(len(e.provider.effects), 2)


if __name__ == "__main__":
    unittest.main()
