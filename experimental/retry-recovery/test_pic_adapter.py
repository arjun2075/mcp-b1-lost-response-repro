"""Acceptance tests for the non-dispatching PIC approval adapter.

Real verification runs inside pic_adapter/worker.py through
pic_adapter/client.py, the way the harness will use it, with every case sent
sequentially through one worker. Proposals are built with the vendored
example's signing helpers, loaded under a private module name so the vendored
directory never joins this process's sys.path. Cases that must alter worker
internals (dispatch spy, injected exception, simulated internal error, SDK
pin) run in a separate interpreter that performs the worker's own start()
sequence. Client failure paths use small fake workers; where a healthy
replacement is needed, later fake starts hand over to the real worker.
"""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

import anyio  # noqa: E402

from pic_adapter import client as client_mod  # noqa: E402
from pic_adapter import verify  # noqa: E402
from pic_adapter.client import VerifierClient, WorkerError, valid_worker_response  # noqa: E402
from pic_adapter.run_vendor_baseline import VENDOR, verify_manifest  # noqa: E402

HERE = Path(__file__).resolve().parent
VENDOR_DIR = verify.VENDOR_DIR
REAL_WORKER = HERE / "pic_adapter" / "worker.py"
PYTHON = sys.executable
MARKER = "ZQX-MARKER-7731"
ZERO_SIG = base64.b64encode(bytes(64)).decode("ascii")
HEALTHY_TIMEOUT_S = 60.0


def _load_signing():
    """Vendored sign_and_verify under a private name; sys.path is not changed."""
    spec = importlib.util.spec_from_file_location("_vendored_sign_and_verify",
                                                  VENDOR_DIR / "sign_and_verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAV = _load_signing()
MD = json.loads((VENDOR_DIR / "fixtures" / "merchant_decision.json").read_text("utf-8"))
EXPECTED = {"tool": MD["action"]["tool"], "impact": MD["impact"],
            "args": copy.deepcopy(MD["action"]["args"])}


def decision(**changes) -> dict:
    d = copy.deepcopy(MD)
    d.update(changes)
    return d


def signed(**changes) -> dict:
    return SAV.build_signed_proposal(merchant_decision=decision(**changes))


def tampered(mutate) -> dict:
    p = signed()
    mutate(p)
    return p


def shell() -> dict:
    return SAV.build_proposal(
        decision_id=MD["decision_id"], approval_ref=MD["approval_ref"],
        action_tool=MD["action"]["tool"], action_args=MD["action"]["args"],
        impact=MD["impact"], signer_key_id=MD["signer_key_id"])


def unsigned_expiry_proposal() -> dict:
    return SAV.sign_proposal(proposal=shell(), signer_key_id=MD["signer_key_id"],
                             expires_at=None)


def untrusted_claim_evidence_proposal() -> dict:
    """Validly signed and bound, but the claim cites provenance that is not trusted.

    Signature and binding succeed; PIC's final contract check then returns
    PIC_VERIFIER_FAILED at the pinned SDK.
    """
    p = shell()
    p["claims"][0]["evidence"] = ["unapproved-provenance"]
    return SAV.sign_proposal(proposal=p, signer_key_id=MD["signer_key_id"],
                             expires_at=MD["expires_at"])


def duplicate_evidence_id() -> dict:
    p = signed()
    bad = copy.deepcopy(p["evidence"][0])
    bad["signature"] = ZERO_SIG
    p["evidence"].append(bad)
    return p


def request(proposal, *, expected=None, keyring="trusted", clock=None, ctx=None) -> dict:
    context = {"keyring_fixture": keyring, "profile": verify.PROFILE}
    if clock is not None:
        context["clock"] = clock
    context.update(ctx or {})
    return {"proposal": proposal, "expected_action": copy.deepcopy(expected or EXPECTED),
            "verification_context": context}


def expected_with(**args) -> dict:
    e = copy.deepcopy(EXPECTED)
    e["args"].update(args)
    return e


def set_path(path, value):
    def mutate(p):
        target = p
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


def build_cases() -> dict:
    return {
        "valid": request(signed()),
        "tamper_args": request(tampered(set_path(["action", "args", "amount_minor"], 9999))),
        "tamper_tool": request(tampered(set_path(["action", "tool"], "refund_v2"))),
        "tamper_impact": request(tampered(set_path(["impact"], "privacy"))),
        "tamper_claim": request(tampered(set_path(["claims", 0, "text"], "{}"))),
        "tamper_intent": request(tampered(lambda p: p.update(intent=p["intent"] + " x"))),
        "tamper_provenance": request(tampered(set_path(["provenance", 0, "id"], "other"))),
        "forged_signature": request(tampered(set_path(["evidence", 0, "signature"], ZERO_SIG))),
        "verifier_failed": request(untrusted_claim_evidence_proposal()),
        "profile_other_tool": request(signed(action={"tool": "payout",
                                                     "args": MD["action"]["args"]})),
        "missing_expiry": request(unsigned_expiry_proposal()),
        "expected_differs": request(signed(), expected=expected_with(amount_minor=3999)),
        "expected_bool_amount": request(signed(), expected=expected_with(amount_minor=True)),
        "expiry_at_boundary": request(signed(), clock="2099-01-01T00:00:00Z"),
        "expiry_after_boundary": request(signed(), clock="2099-01-01T00:00:01Z"),
        "key_before_expiry": request(signed(), keyring="expiring", clock="2029-12-31T23:59:59Z"),
        "key_at_expiry": request(signed(), keyring="expiring", clock="2030-01-01T00:00:00Z"),
        "revoked": request(signed(), keyring="revoked"),
        "untrusted": request(signed(), keyring="empty"),
        "reissued": request(signed(approval_ref="approval-002")),
        "duplicate_evidence_id": request(duplicate_evidence_id()),
        "clock_naive": request(signed(), clock="2030-01-01T00:00:00"),
        "clock_null": request(signed(), ctx={"clock": None}),
        "malformed_not_object": [],
        "malformed_extra_key": {**request(signed()), "extra": 1},
        "malformed_keyring": request(signed(), keyring="production"),
        "malformed_profile": request(signed(), ctx={"profile": "other"}),
        # Rejected inputs carrying a unique marker that must never be echoed.
        "marker_args": request(tampered(set_path(["action", "args", "payment_id"], MARKER))),
        "marker_claim": request(tampered(set_path(["claims", 0, "text"], MARKER))),
        "marker_key_id": request(tampered(set_path(["evidence", 0, "key_id"], MARKER))),
        "marker_profile_tool": request(signed(action={"tool": MARKER,
                                                      "args": MD["action"]["args"]})),
        "marker_expected_args": request(signed(), expected=expected_with(currency=MARKER)),
        "marker_profile_value": request(signed(), ctx={"profile": MARKER}),
        "marker_clock": request(signed(), clock=MARKER),
    }


async def run_batch(cases: dict) -> tuple:
    out = {}
    async with VerifierClient(PYTHON, timeout=HEALTHY_TIMEOUT_S) as client:
        for name, req in cases.items():
            out[name] = await client.verify(req)
    stats = {"starts": client.starts, "terminations": list(client.terminations),
             "cleanup_errors": list(client.cleanup_errors),
             "unconfirmed": len(client.unconfirmed_processes), "proc": client._proc}
    return out, stats


class WorkerAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results, cls.stats = anyio.run(run_batch, build_cases())

    def outcome(self, name, decision_, reason):
        r = self.results[name]
        self.assertEqual((r["decision"], r["reason_code"]), (decision_, reason), r)
        self.assertTrue(valid_worker_response(r), r)
        if decision_ != "allow":
            self.assertIsNone(r["verified_claims"])
            self.assertIsNone(r["verified_action"])
        return r

    def checks(self, name, **expected):
        actual = self.results[name]["checks"]
        self.assertEqual({k: actual[k] for k in expected}, expected, actual)

    def test_all_cases_used_one_worker_without_replacement(self):
        self.assertEqual(self.stats["starts"], 1)
        self.assertEqual(self.stats["terminations"], [])
        self.assertEqual(self.stats["cleanup_errors"], [])
        self.assertEqual(self.stats["unconfirmed"], 0)
        self.assertIsNone(self.stats["proc"])

    def test_valid_proposal_allows(self):
        r = self.outcome("valid", "allow", None)
        self.assertTrue(all(v == "pass" for v in r["checks"].values()))
        self.assertEqual(r["verified_claims"], {"decision_id": "decision-001",
                                                "approval_ref": "approval-001"})
        self.assertEqual(r["verified_action"], EXPECTED)
        self.assertEqual(r["trace"], [])

    def test_signed_binding_tampering_fails_binding(self):
        for name in ("tamper_args", "tamper_tool", "tamper_impact", "tamper_claim",
                     "tamper_intent", "tamper_provenance"):
            with self.subTest(case=name):
                r = self.outcome(name, "deny", "signature_or_binding_rejected")
                self.checks(name, proposal_binding="fail", signature="unavailable",
                            freshness="unavailable", signer_trust="pass",
                            workflow_profile="not_evaluated", expected_action="not_evaluated")
                self.assertIn("pic_error_code=PIC_EVIDENCE_FAILED", r["trace"])

    def test_forged_signature_fails_signature(self):
        self.outcome("forged_signature", "deny", "signature_or_binding_rejected")
        self.checks("forged_signature", signature="fail", proposal_binding="unavailable",
                    freshness="unavailable")

    def test_real_verifier_failed_is_proposal_rejection(self):
        r = self.outcome("verifier_failed", "deny", "proposal_rejected")
        self.assertIn("pic_error_code=PIC_VERIFIER_FAILED", r["trace"])
        self.checks("verifier_failed", signer_trust="pass", signature="unavailable",
                    proposal_binding="unavailable", freshness="unavailable",
                    workflow_profile="not_evaluated")

    def test_profile_rejects_other_tool(self):
        self.outcome("profile_other_tool", "deny", "profile_rejected")
        self.checks("profile_other_tool", signature="pass", proposal_binding="pass",
                    freshness="pass", workflow_profile="fail", expected_action="not_evaluated")

    def test_missing_expiry_is_profile_rejection(self):
        self.outcome("missing_expiry", "deny", "profile_rejected")
        self.checks("missing_expiry", signature="pass", proposal_binding="pass",
                    freshness="not_evaluated", workflow_profile="fail")

    def test_expected_action_mismatch(self):
        self.outcome("expected_differs", "deny", "action_mismatch")
        self.checks("expected_differs", workflow_profile="pass", expected_action="fail")

    def test_boolean_expected_amount_is_malformed_input(self):
        self.outcome("expected_bool_amount", "error", "malformed_input")

    def test_attestation_expiry_boundary(self):
        self.outcome("expiry_at_boundary", "allow", None)
        self.outcome("expiry_after_boundary", "deny", "approval_expired")
        self.checks("expiry_after_boundary", freshness="fail", signature="unavailable",
                    proposal_binding="unavailable", signer_trust="pass")

    def test_key_expiry_boundary(self):
        self.outcome("key_before_expiry", "allow", None)
        self.outcome("key_at_expiry", "deny", "signer_untrusted")
        self.checks("key_at_expiry", signer_trust="fail", signature="unavailable",
                    proposal_binding="unavailable", freshness="unavailable")

    def test_revoked_and_untrusted_signers(self):
        self.outcome("revoked", "deny", "signer_revoked")
        self.outcome("untrusted", "deny", "signer_untrusted")
        for name in ("revoked", "untrusted"):
            self.checks(name, signer_trust="fail", signature="unavailable",
                        proposal_binding="unavailable", freshness="unavailable")

    def test_reissued_approval_reports_new_reference(self):
        r = self.outcome("reissued", "allow", None)
        self.assertEqual(r["verified_claims"], {"decision_id": "decision-001",
                                                "approval_ref": "approval-002"})

    def test_duplicate_evidence_id_withholds_stage_attribution(self):
        r = self.outcome("duplicate_evidence_id", "deny", "evidence_rejected_unclassified")
        self.checks("duplicate_evidence_id", signature="unavailable",
                    proposal_binding="unavailable", freshness="unavailable")
        self.assertIn(verify.AMBIGUOUS_ID_TEXT, r["trace"])

    def test_clock_rejections(self):
        for name in ("clock_naive", "clock_null", "marker_clock"):
            with self.subTest(case=name):
                self.outcome(name, "error", "clock_rejected")

    def test_malformed_requests(self):
        for name in ("malformed_not_object", "malformed_extra_key", "malformed_keyring",
                     "malformed_profile", "marker_profile_value", "marker_expected_args"):
            with self.subTest(case=name):
                self.outcome(name, "error", "malformed_input")

    def test_rejected_input_values_never_echoed(self):
        for name, r in self.results.items():
            if name.startswith("marker_"):
                with self.subTest(case=name):
                    self.assertNotEqual(r["decision"], "allow")
                    self.assertNotIn(MARKER, json.dumps(r))

    def test_every_diagnostic_pattern_was_exercised(self):
        stage_failures = {(n, s) for n, r in self.results.items()
                          for s, v in r["checks"].items() if v == "fail"}
        for case, stage in (("expiry_after_boundary", "freshness"),
                            ("tamper_args", "proposal_binding"),
                            ("tamper_tool", "proposal_binding"),
                            ("tamper_provenance", "proposal_binding"),
                            ("forged_signature", "signature")):
            self.assertIn((case, stage), stage_failures)

    def test_harness_process_never_loaded_vendored_names(self):
        self.assertNotIn(str(VENDOR_DIR), sys.path)
        self.assertNotIn("integration", sys.modules)
        self.assertNotIn("sign_and_verify", sys.modules)
        evidence = sys.modules.get("pic_standard.evidence")
        if evidence is not None:
            import datetime
            self.assertIs(evidence.datetime, datetime.datetime)


IN_WORKER_PRELUDE = textwrap.dedent(f"""
    import json, sys
    sys.dont_write_bytecode = True
    sys.path.insert(0, {str(HERE)!r})
    from pic_adapter import worker, verify
    verify_approval, clock_is_clear, _ = worker.start()
    REQUEST = json.load(sys.stdin)
""")


def run_in_worker_process(body: str, req: dict) -> dict:
    proc = subprocess.run([PYTHON, "-B", "-c", IN_WORKER_PRELUDE + textwrap.dedent(body)],
                          input=json.dumps(req), capture_output=True, text=True,
                          timeout=120, cwd=str(HERE), env=client_mod._clean_env())
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class InWorkerProcessTests(unittest.TestCase):
    def test_verification_never_calls_dispatch(self):
        out = run_in_worker_process("""
            import integration
            calls = []
            def spy(**kwargs):
                calls.append(kwargs)
                raise AssertionError("dispatch attempted")
            integration.verify_and_dispatch = spy
            r = verify_approval(REQUEST)
            print(json.dumps({"decision": r["decision"], "calls": len(calls),
                              "clear": clock_is_clear()}))
        """, request(signed()))
        self.assertEqual(out, {"decision": "allow", "calls": 0, "clear": True})

    def test_injected_exception_under_clock_restores_clock(self):
        out = run_in_worker_process(f"""
            import pic_standard.pipeline as pipeline
            def boom(*args, **kwargs):
                raise RuntimeError({MARKER!r})
            pipeline.verify_proposal = boom
            r = verify_approval(REQUEST)
            print(json.dumps({{"response": r, "clear": clock_is_clear()}}))
        """, request(signed(), clock="2030-01-01T00:00:00Z"))
        r = out["response"]
        self.assertEqual((r["decision"], r["reason_code"]), ("error", "verifier_error"))
        self.assertIn("exception type RuntimeError", r["trace"])
        self.assertNotIn(MARKER, json.dumps(r))
        self.assertTrue(out["clear"])

    def test_simulated_internal_error_is_verifier_error(self):
        # Simulated: PIC_INTERNAL_ERROR is the pipeline's catch-all for unexpected
        # exceptions and is not reachable with well-formed fixture input.
        out = run_in_worker_process(f"""
            import pic_standard.pipeline as pipeline
            from pic_standard.errors import PICError, PICErrorCode
            pipeline.verify_proposal = lambda *a, **k: pipeline.PipelineResult(
                ok=False, error=PICError(code=PICErrorCode.INTERNAL_ERROR, message={MARKER!r}))
            print(json.dumps(verify_approval(REQUEST)))
        """, request(signed()))
        self.assertEqual((out["decision"], out["reason_code"]), ("error", "verifier_error"))
        self.assertIn("pic_error_code=PIC_INTERNAL_ERROR", out["trace"])
        self.assertNotIn(MARKER, json.dumps(out))

    def test_sdk_pin_mismatch_is_error(self):
        out = run_in_worker_process("""
            verify.installed_sdk_commit = lambda: "0" * 40
            print(json.dumps(verify_approval(REQUEST)))
        """, request(signed()))
        self.assertEqual((out["decision"], out["reason_code"]), ("error", "sdk_pin_mismatch"))
        self.assertEqual(out["evidence"]["installed_pic_sdk_commit"], "0" * 40)


class InProcessGuardTests(unittest.TestCase):
    def test_clock_outside_worker_is_rejected(self):
        r = verify.verify_approval(request(signed(), clock="2030-01-01T00:00:00Z"))
        self.assertEqual((r["decision"], r["reason_code"]), ("error", "clock_rejected"))

    def test_vendored_example_not_importable_here(self):
        r = verify.verify_approval(request(signed()))
        self.assertEqual((r["decision"], r["reason_code"]), ("error", "verifier_error"))
        self.assertNotIn("integration", sys.modules)


class FixtureTests(unittest.TestCase):
    def keyring(self, path):
        from pic_standard.keyring import TrustedKeyRing
        return TrustedKeyRing.from_json_file(path)

    def test_trusted_keyring_matches_vendored_example(self):
        ours = self.keyring(verify.KEYRINGS["trusted"])
        theirs = self.keyring(VENDOR_DIR / "keys" / "pic_keys.example.json")
        self.assertEqual((ours.keys, ours.revoked_keys), (theirs.keys, theirs.revoked_keys))

    def test_keyring_statuses(self):
        self.assertEqual(self.keyring(verify.KEYRINGS["revoked"]).key_status(
            "merchant-test-v1"), "revoked")
        self.assertEqual(self.keyring(verify.KEYRINGS["empty"]).key_status(
            "merchant-test-v1"), "missing")

    def test_vendor_manifest_intact(self):
        self.assertEqual(verify_manifest(VENDOR)[1], [])


def allow_response() -> dict:
    return {
        "adapter_contract": verify.CONTRACT, "decision": "allow",
        "checks": dict.fromkeys(verify.CHECKS, "pass"),
        "verified_claims": {"decision_id": "d", "approval_ref": "a"},
        "verified_action": copy.deepcopy(EXPECTED), "reason_code": None, "trace": [],
        "evidence": {"example_commit": verify.EXAMPLE_COMMIT,
                     "pic_sdk_commit": verify.SDK_COMMIT,
                     "installed_pic_sdk_commit": verify.SDK_COMMIT},
    }


def deny_response() -> dict:
    r = allow_response()
    r.update(decision="deny", verified_claims=None, verified_action=None,
             reason_code="action_mismatch", trace=["x"])
    r["checks"]["expected_action"] = "fail"
    return r


class ResponseValidationTests(unittest.TestCase):
    def rejects(self, base, mutate):
        r = base()
        mutate(r)
        self.assertFalse(valid_worker_response(r), r)

    def test_baselines_are_valid(self):
        self.assertTrue(valid_worker_response(allow_response()))
        self.assertTrue(valid_worker_response(deny_response()))

    def test_malformed_reason_codes_rejected_without_raising(self):
        for bad in (["action_mismatch"], {"x": 1}, None, 3, "made_up"):
            with self.subTest(reason=bad):
                self.rejects(deny_response, lambda r: r.update(reason_code=bad))

    def test_incomplete_allowed_action_arguments_rejected(self):
        for args in ({}, {"payment_id": "p", "amount_minor": 1},
                     {"payment_id": "p", "amount_minor": True, "currency": "EUR"},
                     {"payment_id": "p", "amount_minor": 0, "currency": "EUR"},
                     {"payment_id": "", "amount_minor": 1, "currency": "EUR"},
                     {"payment_id": "p", "amount_minor": 1, "currency": "EUR", "x": 1}):
            with self.subTest(args=args):
                self.rejects(allow_response, lambda r: r["verified_action"].update(args=args))

    def test_allow_requires_all_pass_and_null_reason(self):
        self.rejects(allow_response, lambda r: r["checks"].update(signature="fail"))
        self.rejects(allow_response, lambda r: r.update(reason_code="action_mismatch"))
        self.rejects(allow_response, lambda r: r.update(verified_claims=None))

    def test_structure_rejections(self):
        self.rejects(deny_response, lambda r: r.update(verified_claims={"decision_id": "d"}))
        self.rejects(deny_response, lambda r: r["checks"].update(signature="maybe"))
        self.rejects(deny_response, lambda r: r["checks"].update(signature=["pass"]))
        self.rejects(deny_response, lambda r: r.update(trace=[1]))
        self.rejects(deny_response, lambda r: r.update(extra=True))
        self.rejects(deny_response, lambda r: r["evidence"].update(installed_pic_sdk_commit=None))


FAKE_WORKER = textwrap.dedent(f"""
    import json, os, runpy, sys, time
    state = os.environ["FAKE_WORKER_STATE"]
    try:
        with open(state, encoding="utf-8") as f:
            started_before = int(f.read() or 0)
    except FileNotFoundError:
        started_before = 0
    with open(state, "w", encoding="utf-8") as f:
        f.write(str(started_before + 1))
    if started_before and os.environ.get("FAKE_WORKER_THEN_REAL") == "1":
        sys.argv = [{str(REAL_WORKER)!r}]
        runpy.run_path({str(REAL_WORKER)!r}, run_name="__main__")
    mode = os.environ["FAKE_WORKER_MODE"]
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n")
        sys.stdout.flush()
    if mode == "startup_error":
        send({{"type": "startup_error", "protocol": 1, "error": "refused"}})
        sys.exit(3)
    send({{"type": "ready", "protocol": 1, "pid": os.getpid(),
           "installed_pic_sdk_commit": {verify.SDK_COMMIT!r}}})
    for line in sys.stdin:
        m = json.loads(line)
        if m["op"] == "health":
            send({{"type": "health", "request_id": m["request_id"],
                   "ok": mode != "bad_health", "clock_clear": True}})
            continue
        if mode == "hang":
            time.sleep(3600)
        if mode == "crash":
            sys.exit(9)
        if mode == "wrong_id":
            send({{"type": "verify", "request_id": "not-" + m["request_id"], "response": {{}}}})
        if mode == "malformed":
            send({{"type": "verify", "request_id": m["request_id"],
                   "response": {{"decision": "allow"}}}})
""")


class StuckStdin:
    async def send(self, data):
        await anyio.sleep_forever()


class ClientFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fake-worker-"))
        cls.fake = cls.tmp / "fake_worker.py"
        cls.fake.write_text(FAKE_WORKER, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def with_fake(self, mode, coro_fn, *, then_real=False):
        env = {"FAKE_WORKER_MODE": mode,
               "FAKE_WORKER_STATE": str(self.tmp / f"state-{uuid.uuid4().hex}"),
               "FAKE_WORKER_THEN_REAL": "1" if then_real else "0"}
        with mock.patch.object(client_mod, "WORKER", self.fake), \
                mock.patch.dict(os.environ, env):
            return anyio.run(coro_fn)

    def replacement_run(self, mode, timeout, *, cancel_first=False):
        """Fail one request against a fake worker, then verify through a fresh real one."""
        async def go():
            c = VerifierClient(PYTHON, timeout=timeout, startup_timeout=HEALTHY_TIMEOUT_S)
            first, cancelled = None, False
            try:
                await c.start()
                pid_first = c.ready_info["pid"]
                if cancel_first:
                    with anyio.move_on_after(1.0) as scope:
                        first = await c.verify(request(signed()))
                    cancelled = scope.cancelled_caught
                else:
                    first = await c.verify(request(signed()))
                c.timeout = HEALTHY_TIMEOUT_S   # the healthy replacement is not under test
                second = await c.verify(request(signed(approval_ref="approval-002")))
                pid_second = c.ready_info["pid"]
            finally:
                await c.close()
            return {"first": first, "cancelled": cancelled, "second": second,
                    "pids": (pid_first, pid_second), "client": c}
        return self.with_fake(mode, go, then_real=True)

    def assert_healthy_replacement(self, run, termination):
        c = run["client"]
        self.assertEqual(c.terminations, [termination])
        self.assertEqual(c.starts, 2)
        self.assertNotEqual(run["pids"][0], run["pids"][1])
        second = run["second"]
        self.assertEqual(second["decision"], "allow", second)
        self.assertTrue(valid_worker_response(second))
        # A fresh process returned the follow-up approval's own result.
        self.assertEqual(second["verified_claims"]["approval_ref"], "approval-002")
        self.assertEqual(c.cleanup_errors, [])
        self.assertEqual(c.unconfirmed_processes, [])
        self.assertIsNone(c._proc)

    def assert_unavailable(self, response, problem):
        self.assertEqual((response["decision"], response["reason_code"]),
                         ("error", "verifier_unavailable"))
        self.assertIn(problem, response["trace"])

    def test_timeout_then_healthy_replacement(self):
        run = self.replacement_run("hang", timeout=1)
        self.assertFalse(run["cancelled"])
        self.assert_unavailable(run["first"], "timeout")
        self.assert_healthy_replacement(run, "timeout")

    def test_malformed_reply_then_healthy_replacement(self):
        run = self.replacement_run("malformed", timeout=10)
        self.assertFalse(run["cancelled"])
        self.assert_unavailable(run["first"], "malformed verification response")
        self.assert_healthy_replacement(run, "malformed verification response")

    def test_cancellation_then_healthy_replacement(self):
        run = self.replacement_run("hang", timeout=HEALTHY_TIMEOUT_S, cancel_first=True)
        self.assertTrue(run["cancelled"])
        self.assertIsNone(run["first"], "cancellation must not produce a verification response")
        self.assert_healthy_replacement(run, "cancelled with a request in flight")

    def failure(self, mode, problem):
        async def go():
            async with VerifierClient(PYTHON, timeout=10, startup_timeout=30) as c:
                return await c.verify(request(signed())), list(c.terminations)
        r, terminations = self.with_fake(mode, go)
        self.assert_unavailable(r, problem)
        self.assertEqual(terminations, [problem])

    def test_crash_is_unavailable(self):
        self.failure("crash", "worker exited or reply unreadable")

    def test_mismatched_request_id_is_unavailable(self):
        self.failure("wrong_id", "reply request_id mismatch")

    def test_refused_startup_and_failed_first_health(self):
        for mode in ("startup_error", "bad_health"):
            with self.subTest(mode=mode):
                async def go():
                    c = VerifierClient(PYTHON, startup_timeout=30)
                    try:
                        with self.assertRaises(WorkerError):
                            await c.start()
                        return await c.verify(request(signed())), c._proc
                    finally:
                        await c.close()
                r, proc = self.with_fake(mode, go)
                self.assert_unavailable(r, "worker could not be started")
                self.assertIsNone(proc)

    def test_missing_interpreter_is_unavailable(self):
        async def go():
            c = VerifierClient(str(self.tmp / "no-such-python.exe"))
            try:
                return await c.verify(request(signed())), c.starts
            finally:
                await c.close()
        r, starts = anyio.run(go)
        self.assert_unavailable(r, "worker could not be started")
        self.assertEqual(starts, 0)

    def test_blocked_send_times_out(self):
        class KillableProc:
            def __init__(self):
                self.returncode = None
                self.stdin = StuckStdin()
                self.killed = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

            async def aclose(self):
                pass

        async def go():
            c = VerifierClient(PYTHON, timeout=0.5)
            proc = KillableProc()
            c._proc = proc
            result = await c._call({"request_id": "r", "op": "health"}, "health")
            return result, c, proc
        result, c, proc = anyio.run(go)
        self.assertEqual(result, (None, "timeout"))
        self.assertTrue(proc.killed)
        self.assertEqual(c.terminations, ["timeout"])
        self.assertEqual((c.cleanup_errors, c.unconfirmed_processes), ([], []))
        self.assertIsNone(c._proc)

    def test_cleanup_exceptions_during_cancellation_do_not_mask_it(self):
        class BrokenProc:
            returncode = None
            stdin = StuckStdin()

            def kill(self):
                raise PermissionError("denied")

            async def wait(self):
                raise RuntimeError("wait failed")

            async def aclose(self):
                raise RuntimeError("aclose failed")

        async def go():
            c = VerifierClient(PYTHON, timeout=60)
            proc = BrokenProc()
            c._proc = proc
            with anyio.move_on_after(0.5) as scope:
                await c._call({"request_id": "r", "op": "health"}, "health")
            return scope.cancelled_caught, c, proc
        caught, c, proc = anyio.run(go)
        self.assertTrue(caught)
        self.assertEqual(c.terminations, ["cancelled with a request in flight"])
        self.assertEqual(c.cleanup_errors, ["kill: PermissionError", "wait: RuntimeError",
                                            "aclose: RuntimeError",
                                            "termination not confirmed; process handle retained"])
        self.assertEqual(c.unconfirmed_processes, [proc])
        self.assertIsNone(c._proc)


if __name__ == "__main__":
    unittest.main()
