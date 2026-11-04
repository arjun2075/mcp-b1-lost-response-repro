"""Regression test: run_cases.py refuses to run with assertions disabled.

Scenario validation in run_cases.py uses assert statements, which python -O and
PYTHONOPTIMIZE remove. The runner must refuse before running any scenario or
writing results, rather than report unchecked results as passed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REFUSAL = "refuses to run with assertions disabled"


def run_runner(flags, extra_env, output: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONDONTWRITEBYTECODE="1", **extra_env)
    return subprocess.run([sys.executable, "-B", *flags, "run_cases.py", "--output", str(output)],
                          cwd=str(HERE), env=env, capture_output=True, text=True, timeout=120)


class OptimizedModeRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run-cases-optimized-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def assert_refused(self, flags, extra_env):
        output = self.tmp / "results.json"
        self.assertFalse(output.exists())
        proc = run_runner(flags, extra_env, output)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)
        self.assertNotIn("cases passed", proc.stdout)
        self.assertFalse(output.exists(), "no new results may be written")

    def test_refuses_under_dash_o(self):
        self.assert_refused(["-O"], {})

    def test_refuses_under_pythonoptimize(self):
        self.assert_refused([], {"PYTHONOPTIMIZE": "1"})


if __name__ == "__main__":
    unittest.main()
