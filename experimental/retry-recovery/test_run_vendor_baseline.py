"""Focused tests for pic_adapter/run_vendor_baseline.py.

Fake vendored trees and fake interpreter probes exercise the runner's
manifest, setup, result-validation, failure, timeout and termination paths
quickly, without a second PIC installation. The real vendored manifest is
verified read-only; the real upstream checks run only through the runner.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

from pic_adapter import run_vendor_baseline as rvb  # noqa: E402

PIN = "a" * 40
UPSTREAM = "b" * 40
IDS = [f"case-{i:02d}" for i in range(rvb.EXPECTED_CASES)]

GOOD_RESULTS = {
    "cases": [{"id": i, "match": True} for i in IDS],
    "suite_integrity": {"ok": True, "probes": [
        {"name": "premature_tool_call", "ok": True},
        {"name": "wrong_args_dispatch", "ok": True}]},
    "environment_status": "verified",
    "declared_pic_commit": PIN,
    "installed_pic_revision": {"commit_id": PIN},
}
GOOD_CASES = ("import pathlib\npathlib.Path('actual-results.json').write_text("
              + repr(json.dumps(GOOD_RESULTS)) + ", encoding='utf-8')\n")
GOOD_SELF = ("for n in " + repr(sorted(rvb.EXPECTED_SELF_CHECKS)) + ":\n"
             "    print('  [OK] ' + n + ' exit=0 (expected 0)')\n")
NO_WRITE = "pass\n"
FAIL = "raise SystemExit(1)\n"
SLEEP = "import time\ntime.sleep(60)\n"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob("*") if p.is_file()}


def build_vendor(root: Path, test_cases: str = GOOD_CASES, self_check: str = GOOD_SELF,
                 edit=None) -> Path:
    vendor = root / "vendor"
    src = vendor / "fake_vendor"
    (src / "fixtures").mkdir(parents=True)
    (src / "test_cases.py").write_text(test_cases, encoding="utf-8")
    (src / "self_check.py").write_text(self_check, encoding="utf-8")
    (src / "actual-results.json").write_text('{"stale": true}\n', encoding="utf-8")
    (src / "fixtures" / "expected-outcomes.json").write_text(
        json.dumps({"cases": [{"id": i} for i in IDS]}), encoding="utf-8")
    files = [{"path": p.relative_to(src).as_posix(), "sha256": sha(p),
              "upstream_path": p.relative_to(src).as_posix()}
             for p in sorted(src.rglob("*")) if p.is_file()]
    manifest = {"manifest_version": 1, "files": files, "pic_sdk_commit": PIN,
                "upstream_commit": UPSTREAM, "vendored_dir": "fake_vendor",
                "upstream_repository": "https://example.invalid/fake"}
    if edit:
        edit(manifest)
    (vendor / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return vendor


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rvb-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self, name):
        prefix = self.tmp / name
        prefix.mkdir(exist_ok=True)
        du = prefix / "direct_url.json"
        du.write_text("{}", encoding="utf-8")
        return {"prefix": str(prefix), "python": "3", "commit_id": PIN,
                "direct_url_path": str(du)}


class ManifestTests(TempCase):
    def errors(self, vendor):
        return rvb.verify_manifest(vendor)[1]

    def test_real_vendored_manifest_is_intact(self):
        self.assertEqual(self.errors(rvb.VENDOR), [])

    def test_fake_manifest_is_valid(self):
        self.assertEqual(self.errors(build_vendor(self.tmp)), [])

    def test_duplicate_path_rejected(self):
        vendor = build_vendor(self.tmp, edit=lambda m: m["files"].append(dict(m["files"][0])))
        self.assertTrue(any("duplicate manifest paths" in e for e in self.errors(vendor)))

    def test_unsafe_paths_rejected(self):
        for n, bad in enumerate(["../x", "/x", r"a\b", "C:/x", "a/./b", "a//b", ""]):
            with self.subTest(path=bad):
                vendor = build_vendor(self.tmp / f"case{n}",
                                      edit=lambda m: m["files"][0].update(path=bad))
                self.assertTrue(any("unsafe manifest path" in e for e in self.errors(vendor)))

    def test_extra_file_rejected(self):
        vendor = build_vendor(self.tmp)
        (vendor / "fake_vendor" / "planted.py").write_text("x = 1\n", encoding="utf-8")
        self.assertIn("extra file not in manifest: planted.py", self.errors(vendor))

    def test_changed_file_rejected(self):
        vendor = build_vendor(self.tmp)
        (vendor / "fake_vendor" / "test_cases.py").write_text("pass  \n", encoding="utf-8")
        self.assertIn("sha256 mismatch: test_cases.py", self.errors(vendor))

    def test_symlink_rejected(self):
        vendor = build_vendor(self.tmp)
        link = vendor / "fake_vendor" / "link.py"
        try:
            os.symlink(vendor / "fake_vendor" / "test_cases.py", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        self.assertIn("symlink in vendored tree: link.py", self.errors(vendor))

    def test_unsupported_version_rejected(self):
        vendor = build_vendor(self.tmp, edit=lambda m: m.update(manifest_version=2))
        self.assertTrue(any("unsupported manifest_version" in e for e in self.errors(vendor)))

    def test_non_object_manifest_rejected(self):
        vendor = build_vendor(self.tmp)
        (vendor / "MANIFEST.json").write_text("[]", encoding="utf-8")
        self.assertTrue(any("must be a JSON object" in e for e in self.errors(vendor)))

    def test_missing_pin_rejected(self):
        vendor = build_vendor(self.tmp, edit=lambda m: m.pop("pic_sdk_commit"))
        self.assertTrue(any("pic_sdk_commit" in e for e in self.errors(vendor)))


class ResultValidationTests(unittest.TestCase):
    def verdict(self, mutate=None, results=None):
        r = copy.deepcopy(GOOD_RESULTS) if results is None else results
        if mutate:
            mutate(r)
        return {name: ok for name, ok, _ in rvb.check_case_results(r, IDS, PIN)}

    IDS_CHECK = "case ids equal pinned expectations"
    MATCH_CHECK = "every case matches"
    PROBE_CHECK = "integrity probes are exactly the expected two, all ok"
    ENV_CHECK = "environment verified at pin"

    def test_good_results_pass(self):
        self.assertTrue(all(self.verdict().values()))

    def test_duplicate_case_id_rejected(self):
        v = self.verdict(lambda r: r["cases"].append(dict(r["cases"][0])))
        self.assertFalse(v[self.IDS_CHECK])

    def test_missing_case_id_rejected(self):
        self.assertFalse(self.verdict(lambda r: r["cases"].pop())[self.IDS_CHECK])

    def test_unexpected_case_id_rejected(self):
        v = self.verdict(lambda r: r["cases"].append({"id": "case-99", "match": True}))
        self.assertFalse(v[self.IDS_CHECK])

    def test_unmatched_case_rejected(self):
        v = self.verdict(lambda r: r["cases"][0].update(match=False))
        self.assertTrue(v[self.IDS_CHECK])
        self.assertFalse(v[self.MATCH_CHECK])

    def test_truthy_non_boolean_match_rejected(self):
        self.assertFalse(self.verdict(lambda r: r["cases"][0].update(match="true"))
                         [self.MATCH_CHECK])

    def test_missing_probe_rejected(self):
        v = self.verdict(lambda r: r["suite_integrity"]["probes"].pop())
        self.assertFalse(v[self.PROBE_CHECK])

    def test_no_probes_rejected(self):
        v = self.verdict(lambda r: r["suite_integrity"].update(probes=[]))
        self.assertFalse(v[self.PROBE_CHECK])

    def test_duplicate_probe_rejected(self):
        v = self.verdict(lambda r: r["suite_integrity"]["probes"].append(
            dict(r["suite_integrity"]["probes"][0])))
        self.assertFalse(v[self.PROBE_CHECK])

    def test_failed_probe_rejected(self):
        v = self.verdict(lambda r: r["suite_integrity"]["probes"][0].update(ok=False))
        self.assertFalse(v[self.PROBE_CHECK])

    def test_failed_suite_integrity_rejected(self):
        v = self.verdict(lambda r: r["suite_integrity"].update(ok=False))
        self.assertFalse(v[self.PROBE_CHECK])

    def test_extra_malformed_probe_entry_rejected(self):
        # Reproduced false pass: [probe, probe, null] satisfied "exactly the expected two".
        v = self.verdict(lambda r: r["suite_integrity"]["probes"].append(None))
        self.assertFalse(v[self.PROBE_CHECK])

    def test_non_list_probes_rejected(self):
        for bad in ({"name": "premature_tool_call", "ok": True}, None, "probes"):
            with self.subTest(probes=bad):
                v = self.verdict(lambda r: r["suite_integrity"].update(probes=bad))
                self.assertFalse(v[self.PROBE_CHECK])

    def test_malformed_case_entry_rejected(self):
        v = self.verdict(lambda r: r["cases"].append(None))
        self.assertFalse(v[self.IDS_CHECK])

    def test_non_list_cases_rejected(self):
        for bad in ({"id": "case-00", "match": True}, None):
            with self.subTest(cases=bad):
                v = self.verdict(lambda r: r.update(cases=bad))
                self.assertFalse(v[self.IDS_CHECK])

    def test_installed_commit_differs_from_pin(self):
        v = self.verdict(lambda r: r["installed_pic_revision"].update(commit_id="c" * 40))
        self.assertFalse(v[self.ENV_CHECK])

    def test_declared_commit_differs_from_pin(self):
        v = self.verdict(lambda r: r.update(declared_pic_commit="c" * 40))
        self.assertFalse(v[self.ENV_CHECK])

    def test_unverified_environment_rejected(self):
        v = self.verdict(lambda r: r.update(environment_status="unverified"))
        self.assertFalse(v[self.ENV_CHECK])

    def test_non_object_results_rejected(self):
        self.assertEqual(self.verdict(results=[]), {"actual-results.json is a JSON object": False})


class SetupTests(TempCase):
    def test_timeout_must_be_positive_finite(self):
        for bad in ["0", "-1", "inf", "nan"]:
            with self.subTest(value=bad), self.assertRaises(argparse.ArgumentTypeError):
                rvb.positive_finite(bad)
        self.assertEqual(rvb.positive_finite("1.5"), 1.5)

    def test_output_inside_vendor_rejected_before_probing(self):
        vendor = build_vendor(self.tmp)
        target = vendor / "fake_vendor" / "out"
        with mock.patch.object(rvb, "probe") as probe:
            code = rvb.main(["--selfcheck-python", sys.executable,
                             "--output-dir", str(target)], vendor=vendor)
        self.assertEqual(code, rvb.INVALID_SETUP)
        probe.assert_not_called()
        self.assertFalse(target.exists())

    def test_shared_installation_rejected(self):
        main_env, self_env = self.env("a"), self.env("b")
        shared = dict(self_env, direct_url_path=main_env["direct_url_path"])
        errors = rvb.check_isolation(main_env, shared)
        self.assertIn("both interpreters share one pic-standard installation", errors)
        self.assertTrue(any("outside its environment" in e for e in errors))

    def test_same_prefix_rejected(self):
        main_env = self.env("a")
        self.assertIn("--selfcheck-python must be a separate environment",
                      rvb.check_isolation(main_env, dict(main_env)))

    def test_isolated_environments_accepted(self):
        self.assertEqual(rvb.check_isolation(self.env("a"), self.env("b")), [])


class RunTests(TempCase):
    def run_main(self, vendor, timeout="20"):
        out = self.tmp / "out"
        envs = [self.env("main"), self.env("self")]
        before = snapshot(vendor)
        with mock.patch.object(rvb, "probe", side_effect=envs):
            code = rvb.main(["--python", sys.executable,
                             "--selfcheck-python", sys.executable,
                             "--output-dir", str(out), "--timeout", timeout],
                            vendor=vendor)
        self.assertEqual(snapshot(vendor), before, "vendored bytes changed")
        runs = list(out.iterdir())
        self.assertEqual(len(runs), 1)
        summary = json.loads((runs[0] / "summary.json").read_text(encoding="utf-8"))
        if summary["work_dir_retained"]:
            self.addCleanup(shutil.rmtree, summary["work_dir_retained"], True)
        return code, summary, runs[0]

    def check(self, summary, name):
        return next(c for c in summary["checks"] if c["name"] == name)

    def test_good_run_passes(self):
        code, summary, run = self.run_main(build_vendor(self.tmp))
        self.assertEqual(code, rvb.PASS, summary)
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["self_check_env_restoration"], "restored")
        self.assertIsNone(summary["work_dir_retained"])
        self.assertTrue((run / "actual-results.json").is_file())

    def test_stale_results_not_archived(self):
        code, summary, run = self.run_main(build_vendor(self.tmp, test_cases=NO_WRITE))
        self.assertEqual(code, rvb.CHECK_FAILED)
        self.assertFalse(self.check(summary, "fresh actual-results.json produced")["ok"])
        self.assertFalse((run / "actual-results.json").exists())

    def test_subprocess_failure_reported(self):
        code, summary, _ = self.run_main(build_vendor(self.tmp, test_cases=FAIL))
        self.assertEqual(code, rvb.CHECK_FAILED)
        self.assertEqual(self.check(summary, "test_cases exit 0")["observed"], 1)
        self.assertTrue(self.check(summary, "manifest intact after run")["ok"])

    def test_test_cases_timeout_stops_run(self):
        code, summary, run = self.run_main(build_vendor(self.tmp, test_cases=SLEEP),
                                           timeout="1")
        self.assertEqual(code, rvb.INCOMPLETE)
        self.assertEqual(summary["timed_out"], ["test_cases"])
        self.assertIn("test_cases", summary["termination"])
        self.assertEqual(summary["self_check_state"], "not_started")
        self.assertFalse((run / "self_check.stdout.txt").exists())
        self.assertIsNotNone(summary["work_dir_retained"])
        self.assertTrue(Path(summary["work_dir_retained"]).is_dir())

    def test_self_check_timeout_marks_restoration_unverified(self):
        code, summary, _ = self.run_main(build_vendor(self.tmp, self_check=SLEEP),
                                         timeout="1")
        self.assertEqual(code, rvb.INCOMPLETE)
        self.assertEqual(summary["timed_out"], ["self_check"])
        self.assertEqual(summary["self_check_env_restoration"], "unverified")
        self.assertTrue(summary["self_check_env_requires_recreation"])

    def test_restoration_error_does_not_skip_manifest_check(self):
        with mock.patch.object(rvb, "check_restoration", side_effect=RuntimeError("boom")):
            code, summary, _ = self.run_main(build_vendor(self.tmp))
        self.assertEqual(code, rvb.CHECK_FAILED)
        self.assertFalse(summary["passed"])
        self.assertTrue(summary["self_check_env_restoration"].startswith(
            "unverified (RuntimeError: boom"))
        self.assertTrue(summary["self_check_env_requires_recreation"])
        self.assertTrue(self.check(summary, "manifest intact after run")["ok"])

    def test_final_manifest_error_preserves_restoration_result(self):
        real = rvb.verify_manifest
        calls = []

        def second_call_raises(vendor):
            calls.append(vendor)
            if len(calls) > 1:
                raise RuntimeError("disk gone")
            return real(vendor)

        with mock.patch.object(rvb, "verify_manifest", side_effect=second_call_raises):
            code, summary, _ = self.run_main(build_vendor(self.tmp))
        self.assertEqual(len(calls), 2)
        self.assertEqual(code, rvb.CHECK_FAILED)
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["self_check_env_restoration"], "restored")
        manifest_check = self.check(summary, "manifest intact after run")
        self.assertFalse(manifest_check["ok"])
        self.assertIn("raised RuntimeError: disk gone", manifest_check["observed"][0])


class TerminationTests(unittest.TestCase):
    def kill(self, run_result=None, run_error=None):
        proc = mock.Mock(pid=4321)
        run = mock.Mock(return_value=run_result, side_effect=run_error)
        with mock.patch.object(rvb.os, "name", "nt"), \
                mock.patch.object(rvb.subprocess, "run", run):
            outcome = rvb.kill_tree(proc)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs.get("timeout"), rvb.KILL_GRACE_S)
        return proc, outcome

    def test_nonzero_taskkill_triggers_direct_kill(self):
        proc, outcome = self.kill(run_result=subprocess.CompletedProcess([], 128))
        proc.kill.assert_called_once()
        self.assertIn("taskkill /T exit 128", outcome)
        self.assertIn("direct kill attempted", outcome)

    def test_taskkill_timeout_triggers_direct_kill(self):
        proc, outcome = self.kill(run_error=subprocess.TimeoutExpired("taskkill", 1))
        proc.kill.assert_called_once()
        self.assertIn("taskkill failed (TimeoutExpired)", outcome)

    def test_successful_taskkill_needs_no_fallback(self):
        proc, outcome = self.kill(run_result=subprocess.CompletedProcess([], 0))
        proc.kill.assert_not_called()
        self.assertEqual(outcome, "taskkill /T exit 0")

    def test_interruption_terminates_tree_and_reraises(self):
        child = mock.Mock()
        child.communicate.side_effect = KeyboardInterrupt
        with mock.patch.object(rvb.subprocess, "Popen", return_value=child), \
                mock.patch.object(rvb, "kill_tree") as kill:
            with self.assertRaises(KeyboardInterrupt):
                rvb.run_tree(sys.executable, ["-c", "pass"], Path.cwd(), 5)
        kill.assert_called_once_with(child)
        child.wait.assert_called_once_with(timeout=rvb.KILL_GRACE_S)


if __name__ == "__main__":
    unittest.main()
