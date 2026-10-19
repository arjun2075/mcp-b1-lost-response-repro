"""Long-lived verification worker for the retry-recovery fixture.

Started by pic_adapter/client.py as a separate subprocess:

    python -B <path>/pic_adapter/worker.py

The process is the isolation boundary for two process-global effects that
must never reach the MCP harness: the vendored example's top-level module
names (integration, sign_and_verify) on sys.path, and the test clock patch
of the pinned SDK (see clock.py). When main() starts, the worker adjusts
sys.path, keeps the original stdout for protocol replies and sends anything
else printed to stderr. It then verifies the vendor manifest, keyring
fixtures, SDK pin and clean clock, puts the vendored directory on sys.path,
opts in to the test clock, and reports readiness. Importing this module has
none of those effects. Afterwards it answers one JSON line per request on
stdin with one JSON line on stdout, strictly in order, echoing request_id.

Protocol (version 1), one ASCII JSON object per line:
  worker -> client at startup:
    {"type": "ready", "protocol": 1, "pid": ..., "installed_pic_sdk_commit": ...}
    {"type": "startup_error", "protocol": 1, "error": "..."}   then exit 3
  client -> worker:
    {"request_id": "<str>", "op": "health"}
    {"request_id": "<str>", "op": "verify", "request": {...}}
  worker -> client:
    {"type": "health", "request_id": ..., "ok": true, "clock_clear": true}
    {"type": "verify", "request_id": ..., "response": {...}}
    {"type": "protocol_error", "request_id": <str or null>, "error": "..."}

A message that cannot be parsed, an unknown op, or a clock found patched at
a health check or after a request ends the worker after reporting it,
because its state can no longer be trusted; the client then replaces it.
The worker never dispatches anything.

Test instrumentation for the retry-recovery fixture; not part of PIC or MCP.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROTOCOL = 1
MAX_LINE_BYTES = 1_048_576
STARTUP_FAILED, PROTOCOL_FAILED = 3, 4

HERE = Path(__file__).resolve().parent
RETRY_ROOT = HERE.parent

_protocol_out = None


def setup_process() -> None:
    """Process-wide setup, done only when the worker actually runs."""
    global _protocol_out
    sys.dont_write_bytecode = True
    # Script execution puts pic_adapter/ itself first on sys.path. Replace it
    # with the parent so modules load only as pic_adapter.<name>; a second,
    # top-level copy of clock.py would carry its own opt-in flag.
    if sys.path and Path(sys.path[0] or ".").resolve() == HERE:
        sys.path.pop(0)
    if str(RETRY_ROOT) not in sys.path:
        sys.path.insert(0, str(RETRY_ROOT))
    # Protocol replies use the original stdout; anything else goes to stderr.
    _protocol_out = sys.stdout
    sys.stdout = sys.stderr


def send(message: dict) -> None:
    _protocol_out.write(json.dumps(message, ensure_ascii=True, sort_keys=True) + "\n")
    _protocol_out.flush()


def start() -> tuple:
    """Prepare verification; return (verify_approval, clock_is_clear, installed commit)."""
    from pic_adapter.run_vendor_baseline import VENDOR, verify_manifest

    manifest, errors = verify_manifest(VENDOR)
    if errors:
        raise RuntimeError(f"vendor manifest invalid: {len(errors)} problem(s)")
    from pic_adapter import verify

    if VENDOR / manifest["vendored_dir"] != verify.VENDOR_DIR:
        raise RuntimeError("manifest vendored_dir does not match verify.VENDOR_DIR")
    missing = sorted(name for name, path in verify.KEYRINGS.items() if not path.is_file())
    if missing:
        raise RuntimeError(f"keyring fixtures missing: {missing}")
    installed = verify.installed_sdk_commit()
    if installed != verify.SDK_COMMIT:
        raise RuntimeError(f"installed pic-standard commit {installed!r} is not the pinned "
                           f"{verify.SDK_COMMIT}")

    from pic_adapter.clock import clock_is_clear, enable_worker_clock

    if not clock_is_clear():
        raise RuntimeError("test clock already patched at startup")
    sys.path.insert(0, str(verify.VENDOR_DIR))
    enable_worker_clock()
    return verify.verify_approval, clock_is_clear, installed


def read_message(stream):
    """Return (message dict | None at EOF, error text | None)."""
    raw = stream.readline(MAX_LINE_BYTES + 1)
    if not raw:
        return None, None
    if len(raw) > MAX_LINE_BYTES:
        return {}, "message exceeds the maximum line length"
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}, "message is not a UTF-8 JSON line"
    if not isinstance(message, dict) or not isinstance(message.get("request_id"), str):
        return {}, "message must be an object with a string request_id"
    return message, None


def main() -> int:
    setup_process()
    try:
        verify_approval, clock_is_clear, installed = start()
    except Exception as e:
        send({"type": "startup_error", "protocol": PROTOCOL,
              "error": f"{type(e).__name__}: {e}"})
        return STARTUP_FAILED
    send({"type": "ready", "protocol": PROTOCOL, "pid": os.getpid(),
          "installed_pic_sdk_commit": installed})

    stdin = sys.stdin.buffer
    while True:
        message, problem = read_message(stdin)
        if message is None:
            return 0
        request_id = message.get("request_id")
        if problem:
            send({"type": "protocol_error", "request_id": request_id, "error": problem})
            return PROTOCOL_FAILED
        op = message.get("op")
        if op == "health" and set(message) == {"request_id", "op"}:
            if not clock_is_clear():
                send({"type": "protocol_error", "request_id": request_id,
                      "error": "test clock found patched at health check"})
                return PROTOCOL_FAILED
            send({"type": "health", "request_id": request_id, "ok": True,
                  "clock_clear": True})
        elif op == "verify" and set(message) == {"request_id", "op", "request"}:
            response = verify_approval(message["request"])
            if not clock_is_clear():
                send({"type": "protocol_error", "request_id": request_id,
                      "error": "test clock still patched after verification"})
                return PROTOCOL_FAILED
            send({"type": "verify", "request_id": request_id, "response": response})
        else:
            send({"type": "protocol_error", "request_id": request_id,
                  "error": "unknown op or unexpected message keys"})
            return PROTOCOL_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
