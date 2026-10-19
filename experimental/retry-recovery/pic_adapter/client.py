"""Async client for the PIC verification worker, used inside the MCP harness.

Runs pic_adapter/worker.py as a long-lived subprocess through anyio, which
the harness already uses, so waiting for a verification never blocks the
event loop. The harness process never imports the vendored example and
never enables the test clock; both stay inside the worker.

Guarantees:
  * One request in flight at a time; each request carries a fresh
    request_id and the reply must echo it with the expected type.
  * Sending and receiving share one per-request timeout. A timeout, crash,
    EOF, unreadable, mismatched or malformed reply yields an adapter-shaped
    response with decision "error" and reason_code "verifier_unavailable".
    The worker is then terminated and the next call starts a fresh one, so
    a late reply can never be read as the result of a later request.
  * Cancellation is not a worker failure: the worker is discarded with
    bounded, cancellation-shielded cleanup, and the cancellation propagates.
    Cleanup failures are recorded in `cleanup_errors`, never raised over the
    original exception. A worker whose exit cannot be confirmed is kept in
    `unconfirmed_processes` instead of being silently dropped.
  * verify() never dispatches and never raises for worker failures.
    start() raises WorkerError when a fresh worker cannot become ready.
  * Worker replies are checked against the agreed response structure. This
    validates the worker's contract; it does not repeat verification.
  * Each verify() call's latency is recorded in `latencies` as evidence,
    not as a pass/fail gate.

The worker starts no child processes, so terminating it directly suffices.

Test instrumentation for the retry-recovery fixture; not part of PIC or MCP.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from pic_adapter.verify import CHECKS, CONTRACT, EXAMPLE_COMMIT, REASON_TEXT, SDK_COMMIT

WORKER = Path(__file__).resolve().parent / "worker.py"
PROTOCOL = 1
MAX_REQUEST_BYTES = 1_048_576          # matches worker.MAX_LINE_BYTES
MAX_REPLY_BYTES = 4 * 1_048_576
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_STARTUP_TIMEOUT_S = 60.0
KILL_GRACE_S = 5.0
STDERR_TAIL_BYTES = 4096
DECISIONS = ("allow", "deny", "error")
STATUSES = ("pass", "fail", "not_evaluated", "unavailable")
RESPONSE_KEYS = {"adapter_contract", "decision", "checks", "verified_claims",
                 "verified_action", "reason_code", "trace", "evidence"}
CLAIM_KEYS = {"decision_id", "approval_ref"}
ACTION_KEYS = {"tool", "impact", "args"}
REFUND_ARG_KEYS = {"payment_id", "amount_minor", "currency"}

CLIENT_TEXT = {
    "verifier_unavailable": "verification worker unavailable; no verification result",
    "malformed_input": "request is not a well-formed verification request",
}


class WorkerError(RuntimeError):
    """A fresh worker could not be started and made ready."""


def client_response(reason: str, detail: str) -> dict:
    """Adapter-shaped error produced by the client itself; nothing was established."""
    return {
        "adapter_contract": CONTRACT,
        "decision": "error",
        "checks": dict.fromkeys(CHECKS, "unavailable"),
        "verified_claims": None,
        "verified_action": None,
        "reason_code": reason,
        "trace": [CLIENT_TEXT[reason], detail],
        "evidence": {"example_commit": EXAMPLE_COMMIT, "pic_sdk_commit": SDK_COMMIT,
                     "installed_pic_sdk_commit": None},
    }


def _positive_finite(value: float, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a finite number greater than zero")
    return float(value)


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               PYTHONIOENCODING="utf-8")
    return env


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _refund_args_shaped(args: Any) -> bool:
    if not isinstance(args, dict) or set(args) != REFUND_ARG_KEYS:
        return False
    amount = args["amount_minor"]
    return (_non_empty_str(args["payment_id"]) and _non_empty_str(args["currency"])
            and isinstance(amount, int) and not isinstance(amount, bool) and amount > 0)


def valid_worker_response(response: Any) -> bool:
    """True when a worker-produced response matches the agreed contract structure."""
    if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
        return False
    if response["adapter_contract"] != CONTRACT or response["decision"] not in DECISIONS:
        return False
    checks = response["checks"]
    if (not isinstance(checks, dict) or set(checks) != set(CHECKS)
            or not all(isinstance(v, str) and v in STATUSES for v in checks.values())):
        return False
    trace = response["trace"]
    if not isinstance(trace, list) or not all(isinstance(t, str) for t in trace):
        return False
    evidence = response["evidence"]
    if (not isinstance(evidence, dict)
            or evidence.get("example_commit") != EXAMPLE_COMMIT
            or evidence.get("pic_sdk_commit") != SDK_COMMIT
            or evidence.get("installed_pic_sdk_commit") != SDK_COMMIT):
        return False
    claims, action, reason = (response["verified_claims"], response["verified_action"],
                              response["reason_code"])
    if response["decision"] == "allow":
        return (all(v == "pass" for v in checks.values())
                and reason is None
                and isinstance(claims, dict) and set(claims) == CLAIM_KEYS
                and all(_non_empty_str(claims[k]) for k in CLAIM_KEYS)
                and isinstance(action, dict) and set(action) == ACTION_KEYS
                and _non_empty_str(action["tool"]) and _non_empty_str(action["impact"])
                and _refund_args_shaped(action["args"]))
    return (claims is None and action is None
            and isinstance(reason, str) and reason in REASON_TEXT)


class VerifierClient:
    """Serialised access to one live verification worker, replaced on failure."""

    def __init__(self, python: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_S,
                 startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S) -> None:
        # Absolute, not resolved: resolving a POSIX venv's python symlink leaves the venv.
        self.python = os.path.abspath(python or sys.executable)
        self.timeout = _positive_finite(timeout, "timeout")
        self.startup_timeout = _positive_finite(startup_timeout, "startup_timeout")
        self.latencies: list[dict] = []
        self.terminations: list[str] = []
        self.cleanup_errors: list[str] = []
        self.unconfirmed_processes: list = []
        self.starts = 0
        self.ready_info: dict | None = None
        self.last_stderr = ""
        self._lock = anyio.Lock()
        self._proc = None
        self._stdout: BufferedByteReceiveStream | None = None
        self._stderr_file = None

    async def __aenter__(self) -> "VerifierClient":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # ---- public API ----

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def health(self) -> bool:
        async with self._lock:
            if not self._alive():
                return False
            return await self._health()

    async def verify(self, request: Any) -> dict:
        async with self._lock:
            started = time.perf_counter()
            message = {"request_id": uuid.uuid4().hex, "op": "verify", "request": request}
            try:
                line = (json.dumps(message, ensure_ascii=True, allow_nan=False)
                        + "\n").encode("ascii")
            except (TypeError, ValueError):
                return self._record(client_response("malformed_input",
                                                    "request is not JSON-serialisable"),
                                    started, "not_serialisable")
            if len(line) > MAX_REQUEST_BYTES:
                return self._record(client_response("malformed_input",
                                                    "request exceeds the worker line limit"),
                                    started, "too_large")
            if not self._alive():
                try:
                    await self._start()
                except WorkerError:
                    return self._record(client_response("verifier_unavailable",
                                                        "worker could not be started"),
                                        started, "start_failed")
            reply, problem = await self._call(message, "verify", line)
            if problem:
                return self._record(client_response("verifier_unavailable", problem),
                                    started, problem)
            response = reply.get("response")
            if not valid_worker_response(response):
                await self._stop("malformed verification response")
                return self._record(client_response("verifier_unavailable",
                                                    "malformed verification response"),
                                    started, "malformed_response")
            return self._record(response, started, "ok")

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._lock:
                if self._alive():
                    with anyio.move_on_after(KILL_GRACE_S):
                        try:
                            await self._proc.stdin.aclose()   # EOF: the worker exits 0
                            await self._proc.wait()
                        except Exception:
                            pass
                await self._stop(None)

    # ---- internals; callers hold the lock ----

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _start(self) -> None:
        await self._stop(None)
        try:
            self._stderr_file = tempfile.TemporaryFile()
            group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                     else {"start_new_session": True})
            self._proc = await anyio.open_process(
                [self.python, "-B", str(WORKER)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self._stderr_file, env=_clean_env(), **group)
            self._stdout = BufferedByteReceiveStream(self._proc.stdout)
            self.starts += 1
            with anyio.fail_after(self.startup_timeout):
                ready = await self._read()
            if (ready.get("type") != "ready" or ready.get("protocol") != PROTOCOL
                    or ready.get("installed_pic_sdk_commit") != SDK_COMMIT):
                raise WorkerError(f"worker refused to start: "
                                  f"{ready.get('error', ready.get('type'))}")
            if not await self._health():
                raise WorkerError("worker failed its first health check")
            self.ready_info = ready
        except anyio.get_cancelled_exc_class():
            await self._stop("cancelled during startup")
            raise
        except WorkerError:
            await self._stop("startup refused")
            raise
        except Exception as e:
            await self._stop("startup failed")
            raise WorkerError(f"worker could not be started ({type(e).__name__})") from None

    async def _health(self) -> bool:
        reply, problem = await self._call({"request_id": uuid.uuid4().hex, "op": "health"},
                                          "health")
        if problem:
            return False
        if reply.get("ok") is True and reply.get("clock_clear") is True:
            return True
        await self._stop("health check failed")
        return False

    async def _read(self) -> dict:
        raw = await self._stdout.receive_until(b"\n", MAX_REPLY_BYTES)
        message = json.loads(raw.decode("ascii"))
        if not isinstance(message, dict):
            raise ValueError("worker reply is not a JSON object")
        return message

    async def _call(self, message: dict, expected_type: str, line: bytes | None = None):
        """Return (reply, None), or (None, fixed problem text) after stopping the worker.

        Cancellation discards the worker, whose reply may still be pending, and
        propagates.
        """
        if line is None:
            line = (json.dumps(message, ensure_ascii=True) + "\n").encode("ascii")
        try:
            with anyio.fail_after(self.timeout):
                await self._proc.stdin.send(line)
                reply = await self._read()
        except anyio.get_cancelled_exc_class():
            await self._stop("cancelled with a request in flight")
            raise
        except TimeoutError:
            await self._stop("timeout")
            return None, "timeout"
        except Exception:
            await self._stop("worker exited or reply unreadable")
            return None, "worker exited or reply unreadable"
        if reply.get("request_id") != message["request_id"]:
            await self._stop("reply request_id mismatch")
            return None, "reply request_id mismatch"
        if reply.get("type") != expected_type:
            await self._stop("worker protocol error")
            return None, "worker protocol error"
        return reply, None

    def _cleanup_failed(self, step: str, error: BaseException) -> None:
        self.cleanup_errors.append(f"{step}: {type(error).__name__}")

    async def _stop(self, reason: str | None) -> None:
        """Bounded, cancellation-shielded teardown that never raises.

        Each step is attempted independently; ordinary failures are recorded in
        cleanup_errors. References are cleared in finally. A process whose exit
        cannot be confirmed is retained in unconfirmed_processes.
        """
        proc, stderr_file = self._proc, self._stderr_file
        try:
            with anyio.CancelScope(shield=True):
                if proc is not None:
                    if reason:
                        self.terminations.append(reason)
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception as e:
                            self._cleanup_failed("kill", e)
                    try:
                        with anyio.move_on_after(KILL_GRACE_S):
                            await proc.wait()
                    except Exception as e:
                        self._cleanup_failed("wait", e)
                    try:
                        with anyio.move_on_after(KILL_GRACE_S):
                            await proc.aclose()
                    except Exception as e:
                        self._cleanup_failed("aclose", e)
                    if proc.returncode is None:
                        self.unconfirmed_processes.append(proc)
                        self.cleanup_errors.append(
                            "termination not confirmed; process handle retained")
                if stderr_file is not None:
                    try:
                        stderr_file.seek(0, os.SEEK_END)
                        size = stderr_file.tell()
                        stderr_file.seek(max(0, size - STDERR_TAIL_BYTES))
                        self.last_stderr = stderr_file.read().decode("utf-8", "replace")
                    except Exception as e:
                        self._cleanup_failed("stderr read", e)
                    try:
                        stderr_file.close()
                    except Exception as e:
                        self._cleanup_failed("stderr close", e)
        finally:
            self._proc = self._stdout = self._stderr_file = None

    def _record(self, response: dict, started: float, outcome: str) -> dict:
        self.latencies.append({"seconds": round(time.perf_counter() - started, 6),
                               "outcome": outcome, "decision": response["decision"]})
        return response
