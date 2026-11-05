"""Regression tests for the scheduling fixture oracle.

The clean fixture must pass all 8 rows with the expected per-attempt
sequences. Each deliberately broken fixture (scheduling_fixture.BREAKS) must
fail the check that guards it, including breakages the earlier oracle (final
batch count plus fault presence) let pass.

Run from the repository root:
    python -m unittest discover -v -s experimental/retry-recovery/scheduling
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

import anyio  # noqa: E402

import scheduling_fixture as fx  # noqa: E402

# Short SDK read timeout for the tests; the archived run uses READ_TIMEOUT_SECS.
TIMEOUT = 0.5


def case(row: str, key: str, store: str = "record-first") -> fx.Case:
    return next(c for c in fx.CASES if (c.row, c.key, c.store) == (row, key, store))


def run(c: fx.Case, *breaks: str) -> dict:
    return anyio.run(fx.run_case, c, frozenset(breaks), TIMEOUT)


def old_oracle_passes(result: dict, c: fx.Case) -> bool:
    """The check this PR replaces: final batch count, the drop happened (S01)
    and attempt 1 looked like a timeout or an error (any MCPError counted)."""
    first = result["attempts"][0]["status"]
    looked_like_timeout = first in ("timeout", "unexpected_exception")
    looked_like_error = first in ("injected_error", "outcome_unknown", "unexpected_error")
    dropped = any(f["event"] == fx.DROPPED for f in result["transport_faults"])
    return (result["observed"] == c.expected
            and (c.fault != "lost" or (dropped and looked_like_timeout))
            and (c.fault != "error" or looked_like_error))


class CleanFixtureTests(unittest.TestCase):
    def test_all_rows_pass_with_expected_sequences(self):
        for c in fx.CASES:
            with self.subTest(row=c.row, key=c.key, store=c.store):
                result = run(c)
                self.assertTrue(result["passed"], result["problems"])
                got = [(a["status"], a["dispatches"], a["effect_delta"], a["batch"])
                       for a in result["attempts"]]
                want = [(a.status, a.dispatches, a.effect_delta, a.batch) for a in c.attempts]
                self.assertEqual(got, want)
                self.assertEqual(fx.ledger_problems(result["ledger"]), [])
                self.assertEqual(len(result["ledger"]), len(fx.ATTENDEES) * c.expected)

    def test_s01_drops_only_the_committed_success_and_replays_it(self):
        result = run(case("S01", "argument"))
        [drop] = result["transport_faults"]
        self.assertEqual(drop["event"], fx.DROPPED)
        self.assertEqual(drop["jsonrpc_id"], result["attempts"][0]["jsonrpc_id"])
        self.assertEqual(drop["payload"], {"batch": "batch-1", "notified": 3, "result": "committed"})
        self.assertEqual(result["attempts"][0]["exception"]["code"], fx.REQUEST_TIMEOUT)
        self.assertEqual(result["attempts"][0]["exception"]["message"], fx.SDK_TIMEOUT)
        self.assertNotIn(drop["jsonrpc_id"], result["forwarded_reply_ids"])
        self.assertEqual(result["attempts"][1]["payload"],
                         {"batch": "batch-1", "notified": 3, "result": "replayed"})

    def test_s02_positive_control_identical_args_distinct_operation_ids(self):
        result = run(case("S02", "operation_id"))
        self.assertTrue(result["passed"], result["problems"])
        a1, a2 = result["attempts"]
        self.assertEqual(a1["arguments"], a2["arguments"])
        self.assertEqual((a1["operation_id"], a2["operation_id"]), ("op-1", "op-2"))
        # The tool itself received the same arguments and both operation ids.
        self.assertEqual([r.pop("operation_id") for r in result["server_received"]],
                         ["op-1", "op-2"])
        self.assertEqual(result["server_received"], [fx.ARGS, fx.ARGS])

    def test_s03_record_first_retry_gets_exact_outcome_unknown(self):
        result = run(case("S03", "operation_id"))
        self.assertEqual(result["attempts"][0]["client_text"],
                         fx.TOOL_ERROR_PREFIX + fx.INJECTED_ERROR)
        self.assertEqual(result["attempts"][1]["client_text"],
                         fx.TOOL_ERROR_PREFIX + fx.OUTCOME_UNKNOWN)


class LedgerOracleTests(unittest.TestCase):
    def rows(self, batch, attendees, **changes):
        return [{"batch": batch, "attendee": a, **fx.ARGS, **changes} for a in attendees]

    def test_complete_batches_pass(self):
        ledger = self.rows("batch-1", fx.ATTENDEES) + self.rows("batch-2", fx.ATTENDEES)
        self.assertEqual(fx.ledger_problems(ledger), [])

    def test_missing_duplicate_extra_and_wrong_args_fail(self):
        for ledger in (
            self.rows("batch-1", fx.ATTENDEES[:2]),
            self.rows("batch-1", fx.ATTENDEES + fx.ATTENDEES[:1]),
            self.rows("batch-1", fx.ATTENDEES + ("attendee-x",)),
            self.rows("batch-1", fx.ATTENDEES[:2]) + self.rows("batch-1", fx.ATTENDEES[2:], start="x"),
        ):
            with self.subTest(ledger=ledger):
                self.assertNotEqual(fx.ledger_problems(ledger), [])


class BrokenFixtureTests(unittest.TestCase):
    """Each breakage must fail the case, with the expected problem."""

    def assert_fails(self, c: fx.Case, breaks: tuple[str, ...], *expected: str,
                     old_oracle_missed: bool = False) -> dict:
        result = run(c, *breaks)
        self.assertFalse(result["passed"], f"{breaks} passed: {result['attempts']}")
        joined = "\n".join(result["problems"])
        for text in expected:
            self.assertIn(text, joined)
        if old_oracle_missed:
            self.assertTrue(old_oracle_passes(result, c), "breakage should fool the old oracle")
        return result

    # Acceptance 1 / inline :175: drop only a successful, committed first response.
    def test_s01_error_before_effect_is_not_dropped(self):
        result = self.assert_fails(case("S01", "argument"), ("error_before_effect",),
                                   "drop refused: first response is a tool error result",
                                   "expected one drop")
        self.assertEqual(result["transport_faults"], [])

    def test_s01_unguarded_drop_of_error_still_fails_per_attempt(self):
        # The old relay: drops the error, the retry commits the only batch.
        self.assert_fails(case("S01", "argument"),
                          ("error_before_effect", "drop_any_first_response"),
                          "attempt 1: dispatches 0, expected 1",
                          "attempt 2: status 'committed', expected 'replayed'",
                          "attempt 2: dispatches 1, expected 0",
                          old_oracle_missed=True)

    def test_s01_jsonrpc_error_is_not_dropped(self):
        self.assert_fails(case("S01", "operation_id"), ("jsonrpc_error_first",),
                          "drop refused: first response is a JSONRPCError",
                          "attempt 1: unexpected MCPError code -32603",
                          "attempt 1: status 'unexpected_exception', expected 'timeout'")

    def test_s01_retry_that_redispatches_fails(self):
        self.assert_fails(case("S01", "operation_id"), ("retry_redispatches",),
                          "attempt 2: dispatches 1, expected 0",
                          "attempt 2: effect_delta 1, expected 0")

    def test_s01_missing_drop_fails(self):
        self.assert_fails(case("S01", "argument"), ("no_drop",),
                          "attempt 1: status 'committed', expected 'timeout'",
                          "expected one drop")

    # Acceptance 4 / inline :229: only the expected dropped-response timeout is a timeout.
    def test_unrelated_mcp_error_on_retry_fails(self):
        self.assert_fails(case("S01", "argument"), ("unrelated_mcp_error_on_retry",),
                          "attempt 2: unexpected MCPError code -32603",
                          old_oracle_missed=True)

    def test_timeout_coded_error_on_retry_fails(self):
        self.assert_fails(case("S01", "operation_id"), ("timeout_code_on_retry",),
                          "attempt 2: unexpected MCPError code -32001",
                          old_oracle_missed=True)

    def test_drop_recorded_but_error_forwarded_fails(self):
        # The relay logs the drop but forwards an error with the SDK's exact text.
        for key in ("argument", "operation_id"):
            with self.subTest(key=key):
                result = self.assert_fails(
                    case("S01", key), ("drop_recorded_but_error_forwarded",),
                    "attempt 1: unexpected MCPError code -32001: Request 'tools/call' timed out",
                    "attempt 1: status 'unexpected_exception', expected 'timeout'")
                self.assertIn(result["attempts"][0]["jsonrpc_id"], result["forwarded_reply_ids"])

    def test_non_mcp_exception_fails_the_case(self):
        self.assert_fails(case("S01", "argument"), ("non_mcp_exception_on_retry",),
                          "attempt 2: unexpected ValidationError",
                          "attempt 2: status 'unexpected_exception', expected 'replayed'")

    def test_unrelated_mcp_error_without_lost_fault_fails(self):
        self.assert_fails(case("S02", "operation_id"), ("unrelated_mcp_error_on_retry",),
                          "attempt 2: status 'unexpected_exception', expected 'committed'")

    # Acceptance 2: S02 sequences and the positive control's inputs.
    def test_s02_positive_control_reusing_operation_id_fails(self):
        self.assert_fails(case("S02", "operation_id"), ("same_operation_id_on_repeat",),
                          "positive control reused an operation id",
                          "attempt 2: dispatches 0, expected 1")

    def test_s02_operation_ids_are_checked_on_the_wire(self):
        # The wire reuses op-1 while the client's record says op-2, and the
        # expectations are relaxed to what that produces: only the wire check fails.
        c = case("S02", "operation_id")
        relaxed = fx.Case(c.row, c.key, c.store, c.fault, c.operation_ids, c.intended,
                          (fx.Attempt("committed", 1, 1, "batch-1"),
                           fx.Attempt("replayed", 0, 0, "batch-1")))
        result = run(relaxed, "wire_operation_id_differs")
        self.assertFalse(result["passed"])
        self.assertEqual(result["problems"], [
            "sent operation ids ['op-1', 'op-1'], expected ['op-1', 'op-2']",
            "positive control reused an operation id: ['op-1', 'op-1']",
        ])

    def test_s02_changed_arguments_fail(self):
        self.assert_fails(case("S02", "argument"), ("changed_args_on_repeat",),
                          "repeat changed the semantic arguments",
                          "attempt 2: status 'committed', expected 'replayed'")

    # Acceptance 3 / inline :264: the specific post-commit error and outcome-unknown.
    def test_s03_record_first_retry_returning_success_fails(self):
        self.assert_fails(case("S03", "argument"), ("unknown_returns_success",),
                          "attempt 2: status 'replayed', expected 'outcome_unknown'",
                          old_oracle_missed=True)

    def test_s03_wrong_first_error_fails(self):
        for store in ("success-only", "record-first"):
            with self.subTest(store=store):
                self.assert_fails(case("S03", "operation_id", store), ("wrong_injected_error",),
                                  "attempt 1: status 'unexpected_error', expected 'injected_error'",
                                  old_oracle_missed=True)

    def test_s03_success_only_retry_that_replays_is_caught(self):
        # Record-first expectations against a success-only server: [1,1] seen, [1,0] expected.
        c = case("S03", "argument", "success-only")
        wrong = fx.Case(c.row, c.key, c.store, c.fault, c.operation_ids, c.intended,
                        fx.ERROR_UNKNOWN)
        result = run(wrong)
        self.assertFalse(result["passed"])
        self.assertIn("attempt 2: dispatches 1, expected 0", "\n".join(result["problems"]))

    # Acceptance 4: every notification row in the independent ledger.
    def test_ledger_breakages_fail(self):
        for breaks, text in (
            ("ledger_missing_row", "attendees ['attendee-1', 'attendee-2'], expected"),
            ("ledger_duplicate_row", "attendees ['attendee-1', 'attendee-1', 'attendee-2'"),
            ("ledger_extra_attendee", "'attendee-uninvited'"),
        ):
            for c in (case("S02", "operation_id"), case("S03", "argument", "success-only")):
                with self.subTest(breaks=breaks, row=c.row, store=c.store):
                    self.assert_fails(c, (breaks,), text, old_oracle_missed=True)

    def test_returned_batch_must_be_the_ledger_batch(self):
        # Results name batch-N, the ledger rows say nbatch-N.
        for c, text in (
            (case("S02", "operation_id"), "attempt 2: batch 'batch-2' is not in the ledger"),
            (case("S02", "argument"), "attempt 2: batch 'batch-1' is not in the ledger"),
            (case("S03", "argument", "success-only"),
             "attempt 2: returned 'batch-2', committed ['nbatch-2']"),
        ):
            with self.subTest(row=c.row, key=c.key, store=c.store):
                self.assert_fails(c, ("ledger_batch_id_differs",), text, old_oracle_missed=True)

    def test_ledger_breakage_blocks_the_s01_drop(self):
        self.assert_fails(case("S01", "argument"), ("ledger_missing_row",),
                          "drop refused: ledger is not one complete batch")

    def test_unknown_break_is_rejected(self):
        with self.assertRaises(ValueError):
            run(case("S01", "argument"), "no_such_break")


if __name__ == "__main__":
    unittest.main()
