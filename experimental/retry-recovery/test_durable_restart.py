"""Fresh-process crash/restart proof for D01, using only stdlib and SQLite."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("durable_restart.py")


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="d01-restart-")
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        self.prefix = [sys.executable, "-B", str(SCRIPT), "--store", str(path / "app.sqlite"),
                       "--oracle", str(path / "provider.sqlite")]
        self.steps = []

    def run_worker(self, *args, crash=None):
        proc = subprocess.run([*self.prefix, *args], text=True, capture_output=True, timeout=10)
        if crash is not None:
            self.assertEqual(proc.returncode, crash, (proc.stdout, proc.stderr))
            result = {"hard_exit": proc.returncode}
        else:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
        self.steps.append({"arguments": list(args), "result": result})
        return result

    def count(self):
        return self.run_worker("--action", "count")["effects"]

    def test_commit_then_hard_exit_refuses_redispatch(self):
        self.run_worker("--crash", "after_effect", crash=37)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.run_worker(), {"status": "unknown", "reason": "retained_indeterminate"})
        self.assertEqual(self.count(), 1)

    def test_authoritative_correlated_lookup_recovers_after_restart(self):
        self.run_worker("--crash", "after_effect", crash=37)
        self.assertEqual(self.run_worker()["status"], "unknown")
        reconciled = self.run_worker("--action", "reconcile")
        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(reconciled["receipt"]["provider_effect_id"], 1)
        self.assertEqual(self.run_worker(), reconciled)
        self.assertEqual(self.count(), 1)

    def test_crash_after_reservation_before_effect_remains_unknown(self):
        self.run_worker("--crash", "after_reservation", crash=36)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.run_worker(), {"status": "unknown", "reason": "retained_indeterminate"})
        self.assertEqual(self.run_worker("--action", "reconcile"),
                         {"status": "unknown", "reason": "lookup_absent_or_ambiguous"})
        self.assertEqual(self.count(), 0)

    def test_completed_receipt_survives_restarting_process(self):
        first = self.run_worker()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(self.run_worker(), first)
        self.assertEqual(self.count(), 1)

    def test_unsafe_commit_without_reservation_duplicates_after_restart(self):
        self.run_worker("--strategy", "unsafe", "--crash", "after_effect", crash=37)
        self.assertEqual(self.count(), 1)
        second = self.run_worker("--strategy", "unsafe")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.count(), 2)
        self.assertEqual(second["receipt"]["provider_effect_id"], 2)

    def test_scope_and_changed_arguments_protected_after_restart(self):
        self.run_worker("--crash", "after_effect", crash=37)
        self.assertEqual(self.run_worker("--principal", "another"), {"status": "denied"})
        self.assertEqual(self.run_worker("--amount", "5000"), {"status": "conflict"})
        self.assertEqual(self.run_worker("--deny-disclose"), {"status": "denied"})
        self.assertEqual(self.run_worker("--operation-id", "operation-2")["status"], "completed")
        self.assertEqual(self.count(), 2)


def scenario_report():
    report = []
    for name in sorted(n for n in dir(RestartTests) if n.startswith("test_")):
        test = RestartTests(methodName=name)
        test.setUp()
        try:
            getattr(test, name)()
            report.append({"case": name, "passed": True, "steps": test.steps})
        finally:
            test.doCleanups()
    return report


if __name__ == "__main__":
    unittest.main()
