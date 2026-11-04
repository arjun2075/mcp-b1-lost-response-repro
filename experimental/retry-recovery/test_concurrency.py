"""Deterministic overlapping-task assertions for C01, no MCP dependency."""
import unittest

import anyio

from concurrency import ConcurrentEngine, Identity

ARGS = ("operation-1", "pay_A", 4000, "EUR")


async def scenario(*, unsafe=False, post_commit_error=False, other_args=None):
    e = ConcurrentEngine(unsafe=unsafe, post_commit_error=post_commit_error)
    out = {}
    first_done, second_done = anyio.Event(), anyio.Event()

    async def request(label, args, done):
        try:
            out[label] = await e.call(*args)
        finally:
            done.set()

    with anyio.fail_after(4):
        async with anyio.create_task_group() as tg:
            tg.start_soon(request, "first", ARGS, first_done)
            await e.provider.first_entered.wait()
            tg.start_soon(request, "second", other_args or ARGS, second_done)
            if unsafe or (other_args is not None and other_args[0] != ARGS[0]):
                await e.provider.second_entered.wait()
                assert not first_done.is_set() and not second_done.is_set()
            else:
                await second_done.wait()
                assert not first_done.is_set()
            effects_while_held = len(e.provider.effects)
            e.provider.release.set()
            await first_done.wait()
            await second_done.wait()
    return e, out, effects_while_held


class ConcurrencyTests(unittest.TestCase):
    def test_reserved_overlap_blocks_second_dispatch_and_recovers(self):
        e, out, before = anyio.run(scenario)
        self.assertEqual(before, 0)
        self.assertEqual(e.provider.attempts, 1)
        self.assertEqual(out["second"], {"status": "unknown", "reason": "in_progress_or_indeterminate"})
        self.assertEqual(out["first"]["status"], "completed")
        self.assertEqual(len(e.provider.effects), 1)
        self.assertEqual(anyio.run(lambda: e.call(*ARGS)), out["first"])
        self.assertEqual(len(e.provider.effects), 1)

    def test_unlocked_check_then_act_duplicates(self):
        e, out, before = anyio.run(lambda: scenario(unsafe=True))
        self.assertEqual(before, 0)
        self.assertEqual(e.provider.attempts, 2)
        self.assertEqual([out[x]["status"] for x in ("first", "second")], ["completed", "completed"])
        self.assertEqual(len(e.provider.effects), 2)
        self.assertNotEqual(out["first"]["receipt"], out["second"]["receipt"])

    def test_post_commit_unknown_does_not_redispatch(self):
        e, out, before = anyio.run(lambda: scenario(post_commit_error=True))
        self.assertEqual(before, 0)
        self.assertEqual(out["first"], {"status": "execution_error", "outcome": "unknown"})
        self.assertEqual(out["second"]["status"], "unknown")
        self.assertEqual(anyio.run(lambda: e.call(*ARGS))["status"], "unknown")
        self.assertEqual(len(e.provider.effects), 1)

    def test_changed_arguments_conflict_during_inflight(self):
        e, out, before = anyio.run(lambda: scenario(other_args=("operation-1", "pay_A", 5000, "EUR")))
        self.assertEqual(out["second"], {"status": "conflict"})
        self.assertEqual(out["first"]["status"], "completed")
        self.assertEqual(len(e.provider.effects), 1)

    def test_distinct_operations_admitted(self):
        e, out, before = anyio.run(lambda: scenario(other_args=("operation-2", "pay_A", 4000, "EUR")))
        self.assertEqual(before, 0)
        self.assertEqual(len(e.provider.effects), 2)
        self.assertEqual(e.provider.attempts, 2)
        self.assertNotEqual(out["first"]["receipt"], out["second"]["receipt"])

    def test_foreign_context_does_not_receive_original_receipt(self):
        async def exercise():
            e = ConcurrentEngine()
            e.provider.release.set()
            first = await e.call(*ARGS)
            foreign = await e.call(*ARGS, identity=Identity(principal="another"))
            withheld = await e.call(*ARGS, identity=Identity(disclose=False))
            return e, first, foreign, withheld
        e, first, foreign, withheld = anyio.run(exercise)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(foreign, {"status": "denied"})
        self.assertEqual(withheld, {"status": "denied"})
        self.assertEqual(len(e.provider.effects), 1)


if __name__ == "__main__":
    unittest.main()
