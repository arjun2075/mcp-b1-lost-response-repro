"""Run the vendored PIC refund example's own checks without touching its bytes.

Verifies vendor/MANIFEST.json strictly, copies the vendored example to a
disposable directory, runs its test_cases.py there, then runs its
self_check.py serially under a separate, dedicated interpreter, because
self_check.py temporarily rewrites that environment's pic-standard
direct_url.json. Generated output is kept outside the vendored tree.

A timeout or interruption stops the run at that stage: process-tree
termination is attempted with bounded waits, available output is saved,
no further stage starts, and the temporary work tree is retained rather
than deleted under a possibly surviving process. Final checks then run
independently and summary.json is written whenever the output directory
is writable.

Exit status:
  0  every check passed
  1  the run completed but at least one check failed
  2  invalid setup (arguments, manifest, expectations, output location,
     interpreters, isolation); no subprocess stage was started
  3  an interpreter does not have pic-standard at the manifest pin
  4  the run did not complete (timeout, exception or unwritable summary).
     If self_check.py was interrupted, its environment's metadata
     restoration is unverified and that environment must be recreated.

Test instrumentation for the retry-recovery fixture; not part of PIC or MCP.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
DEFAULT_OUT = HERE.parent / "local-results" / "vendor-baseline"
MANIFEST_VERSION = 1
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
EXPECTED_CASES = 22
EXPECTED_PROBES = {"premature_tool_call", "wrong_args_dispatch"}
EXPECTED_SELF_CHECKS = {
    "coverage: empty cases list",
    "coverage: dropped last expectation",
    "coverage: duplicate id",
    "environment: wrong commit_id",
    "environment: direct_url.json missing",
    "environment: dir_info (no vcs_info)",
    "normal run after self-check",
}
DEFAULT_TIMEOUT_S = 600.0
PROBE_TIMEOUT_S = 60.0
KILL_GRACE_S = 30.0
PASS, CHECK_FAILED, INVALID_SETUP, PIN_MISMATCH, INCOMPLETE = 0, 1, 2, 3, 4

PROBE = (
    "import importlib.metadata as m, json, sys\n"
    "d = m.distribution('pic-standard')\n"
    "f = next((x for x in d.files or [] if x.name == 'direct_url.json'), None)\n"
    "print(json.dumps({'prefix': sys.prefix, 'python': sys.version.split()[0],\n"
    "                  'direct_url': d.read_text('direct_url.json'),\n"
    "                  'direct_url_path': str(d.locate_file(f)) if f else None}))\n"
)


@dataclass
class Proc:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    termination: str | None = None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return value


def safe_relative(p: object) -> bool:
    """Plain forward-slash relative path with no empty, '.' or '..' parts."""
    if not isinstance(p, str) or not p or "\\" in p or ":" in p or p.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in p.split("/"))


def verify_manifest(vendor: Path) -> tuple[dict | None, list[str]]:
    """Return (manifest, errors). Any error means the vendored tree is not trusted."""
    try:
        manifest = json.loads((vendor / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, [f"unreadable manifest: {type(e).__name__}: {e}"]
    if not isinstance(manifest, dict):
        return None, [f"manifest must be a JSON object, got {type(manifest).__name__}"]
    errors = []
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"unsupported manifest_version: {manifest.get('manifest_version')!r}")
    for key in ("upstream_commit", "pic_sdk_commit"):
        if not isinstance(manifest.get(key), str) or not COMMIT_RE.fullmatch(manifest[key]):
            errors.append(f"{key} must be a 40-character lowercase hex commit")
    if not isinstance(manifest.get("upstream_repository"), str):
        errors.append("upstream_repository must be a string")
    name = manifest.get("vendored_dir")
    if not safe_relative(name) or "/" in name:
        return manifest, errors + [f"invalid vendored_dir: {name!r}"]
    root = vendor / name
    if root.is_symlink() or not root.is_dir():
        return manifest, errors + [f"vendored_dir is not a real directory: {name}"]

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        return manifest, errors + ["manifest has no files list"]
    safe = []
    for f in entries:
        p = f.get("path") if isinstance(f, dict) else None
        if not safe_relative(p):
            errors.append(f"unsafe manifest path: {p!r}")
        elif not isinstance(f.get("sha256"), str) or not SHA256_RE.fullmatch(f["sha256"]):
            errors.append(f"invalid sha256 for {p}")
        else:
            safe.append(f)
    paths = [f["path"] for f in safe]
    dups = sorted(p for p, n in collections.Counter(paths).items() if n > 1)
    if dups:
        errors.append(f"duplicate manifest paths: {dups}")

    try:
        actual = set()
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                errors.append(f"symlink in vendored tree: {rel}")
            elif p.is_file():
                actual.add(rel)
        for rel in sorted(actual - set(paths)):
            errors.append(f"extra file not in manifest: {rel}")
        for f in safe:
            target = root / f["path"]
            if not target.is_file() or target.is_symlink():
                errors.append(f"missing file: {f['path']}")
            elif sha256(target) != f["sha256"]:
                errors.append(f"sha256 mismatch: {f['path']}")
    except OSError as e:
        errors.append(f"vendored tree unreadable: {type(e).__name__}: {e}")
    return manifest, errors


def load_expected_ids(src: Path) -> list[str]:
    data = json.loads((src / "fixtures" / "expected-outcomes.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("expected-outcomes.json must be an object with a cases list")
    ids = [c.get("id") if isinstance(c, dict) else None for c in data["cases"]]
    if not all(isinstance(i, str) and i for i in ids):
        raise ValueError("every expected case needs a non-empty string id")
    if len(ids) != EXPECTED_CASES or len(set(ids)) != EXPECTED_CASES:
        raise ValueError(f"expected {EXPECTED_CASES} unique case ids, found {len(set(ids))}"
                         f" unique of {len(ids)}")
    return ids


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               PYTHONIOENCODING="utf-8")
    return env


def kill_tree(p: subprocess.Popen) -> str:
    """Bounded attempt to terminate p and its descendants; returns what was done."""
    if os.name == "nt":
        try:
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True, timeout=KILL_GRACE_S, check=False)
        except (subprocess.TimeoutExpired, OSError) as e:
            outcome = f"taskkill failed ({type(e).__name__})"
        else:
            if r.returncode == 0:
                return "taskkill /T exit 0"
            outcome = f"taskkill /T exit {r.returncode}"
        try:
            p.kill()
            return f"{outcome}; direct kill attempted"
        except OSError as e:
            return f"{outcome}; direct kill failed ({type(e).__name__})"
    try:
        os.killpg(p.pid, signal.SIGKILL)
        return "killpg SIGKILL sent"
    except ProcessLookupError:
        return "process group already gone"
    except OSError as e:
        return f"killpg failed ({type(e).__name__})"


def run_tree(python: str, args: list[str], cwd: Path, timeout: float) -> Proc:
    """Run in its own process group; on timeout or interruption kill the whole tree."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    p = subprocess.Popen([python, "-B", *args], cwd=str(cwd), env=clean_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", **group)
    try:
        out, err = p.communicate(timeout=timeout)
        return Proc(p.returncode, out, err, False)
    except subprocess.TimeoutExpired:
        termination = kill_tree(p)
        try:
            out, err = p.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            out, err = "", "(output unavailable: pipes still held after kill)"
        return Proc(None, out or "", err or "", True, termination)
    except BaseException:
        kill_tree(p)
        try:
            p.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        raise


def probe(python: str) -> dict:
    if not os.path.isfile(python):
        raise RuntimeError(f"interpreter not found: {python}")
    proc = run_tree(python, ["-c", PROBE], HERE, PROBE_TIMEOUT_S)
    if proc.timed_out or proc.returncode != 0:
        raise RuntimeError(f"cannot inspect pic-standard under {python}: {proc.stderr.strip()}")
    try:
        info = json.loads(proc.stdout)
        du = json.loads(info["direct_url"]) if info["direct_url"] else {}
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"unexpected probe output from {python}: {e}") from e
    info["commit_id"] = (du.get("vcs_info") or {}).get("commit_id")
    return info


def check_isolation(main_env: dict, self_env: dict) -> list[str]:
    errors = []
    if Path(main_env["prefix"]).resolve() == Path(self_env["prefix"]).resolve():
        errors.append("--selfcheck-python must be a separate environment")
    m, s = main_env.get("direct_url_path"), self_env.get("direct_url_path")
    if not m or not s:
        return errors + ["pic-standard direct_url.json not found in both environments"]
    mp, sp = Path(m).resolve(), Path(s).resolve()
    if mp == sp:
        errors.append("both interpreters share one pic-standard installation")
    if Path(self_env["prefix"]).resolve() not in sp.parents:
        errors.append(f"self-check pic-standard metadata is outside its environment: {sp}")
    return errors


def check_case_results(r: object, expected_ids: list[str], pin: str) -> list[tuple]:
    if not isinstance(r, dict):
        return [("actual-results.json is a JSON object", False, type(r).__name__)]
    # Validate the original lists before filtering: a malformed entry (for example
    # null) must fail the check, not be silently dropped.
    raw_cases = r.get("cases")
    cases = [c for c in raw_cases if isinstance(c, dict)] if isinstance(raw_cases, list) else []
    cases_well_formed = isinstance(raw_cases, list) and len(cases) == len(raw_cases)
    ids = [c.get("id") for c in cases]
    dups = sorted(str(i) for i, n in collections.Counter(ids).items() if n > 1)
    missing = sorted(set(expected_ids) - set(ids))
    unexpected = sorted(str(i) for i in set(ids) - set(expected_ids))
    unmatched = sorted(str(c.get("id")) for c in cases if c.get("match") is not True)
    integrity = r.get("suite_integrity") if isinstance(r.get("suite_integrity"), dict) else {}
    raw_probes = integrity.get("probes")
    probes = [p for p in raw_probes if isinstance(p, dict)] if isinstance(raw_probes, list) else []
    probes_well_formed = (isinstance(raw_probes, list)
                          and len(raw_probes) == len(EXPECTED_PROBES)
                          and len(probes) == len(raw_probes))
    names = [p.get("name") for p in probes]
    not_ok = sorted(str(p.get("name")) for p in probes if p.get("ok") is not True)
    revision = r.get("installed_pic_revision")
    installed = revision.get("commit_id") if isinstance(revision, dict) else None
    return [
        ("case ids equal pinned expectations",
         cases_well_formed and not dups and not missing and not unexpected,
         {"entries": len(raw_cases) if isinstance(raw_cases, list) else None,
          "duplicates": dups, "missing": missing, "unexpected": unexpected}),
        ("every case matches", bool(cases) and not unmatched, unmatched),
        ("integrity probes are exactly the expected two, all ok",
         probes_well_formed and sorted(map(str, names)) == sorted(EXPECTED_PROBES)
         and not not_ok and integrity.get("ok") is True,
         {"entries": len(raw_probes) if isinstance(raw_probes, list) else None,
          "names": names, "not_ok": not_ok}),
        ("environment verified at pin",
         r.get("environment_status") == "verified"
         and r.get("declared_pic_commit") == pin and installed == pin,
         {"status": r.get("environment_status"),
          "declared": r.get("declared_pic_commit"), "installed": installed}),
    ]


def check_self_check(stdout: str) -> tuple:
    ok, failed = [], []
    for line in stdout.splitlines():
        s = line.strip()
        for tag, bucket in (("[OK] ", ok), ("[FAIL] ", failed)):
            if s.startswith(tag):
                bucket.append(s[len(tag):].split(" exit=")[0])
    return ("self-check outcomes are exactly the expected seven, none failed",
            sorted(ok) == sorted(EXPECTED_SELF_CHECKS) and not failed,
            {"ok": ok, "failed": failed})


def check_restoration(state: str, path: Path, before: str) -> dict:
    """Observe only. Never rewrites metadata a surviving process might still touch."""
    if state not in ("not_started", "completed"):
        return {"status": "unverified", "requires_recreation": True}
    after = sha256(path) if path.is_file() else None
    if after == before:
        return {"status": "untouched" if state == "not_started" else "restored",
                "requires_recreation": False}
    return {"status": "changed", "requires_recreation": True}


def save(out_dir: Path, name: str, proc: Proc) -> None:
    (out_dir / f"{name}.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (out_dir / f"{name}.stderr.txt").write_text(proc.stderr, encoding="utf-8")


def fail_setup(code: int, *lines: str) -> int:
    print("[SETUP FAIL]", *lines, sep="\n  ", file=sys.stderr)
    return code


def main(argv: list[str] | None = None, vendor: Path = VENDOR) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter for test_cases.py (default: this one)")
    parser.add_argument("--selfcheck-python", required=True,
                        help="dedicated interpreter for self_check.py; its "
                             "direct_url.json is rewritten and restored")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout", type=positive_finite, default=DEFAULT_TIMEOUT_S,
                        help="per-subprocess timeout in seconds")
    a = parser.parse_args(argv)

    # ---- setup: nothing below starts a stage until every check passes ----
    vendor = vendor.resolve()
    out_root = a.output_dir.resolve()
    if out_root == vendor or vendor in out_root.parents:
        return fail_setup(INVALID_SETUP, f"--output-dir is inside the vendor tree: {out_root}")
    manifest, errors = verify_manifest(vendor)
    if errors:
        return fail_setup(INVALID_SETUP, "manifest invalid before run", *errors)
    pin = manifest["pic_sdk_commit"]
    src = vendor / manifest["vendored_dir"]
    try:
        expected_ids = load_expected_ids(src)
    except (OSError, ValueError) as e:
        return fail_setup(INVALID_SETUP, f"unusable expectations: {type(e).__name__}: {e}")

    # Absolute, not resolved: resolving a POSIX venv's python symlink leaves the venv.
    python, self_python = os.path.abspath(a.python), os.path.abspath(a.selfcheck_python)
    try:
        main_env, self_env = probe(python), probe(self_python)
    except (RuntimeError, OSError) as e:
        return fail_setup(INVALID_SETUP, str(e))
    errors = check_isolation(main_env, self_env)
    if errors:
        return fail_setup(INVALID_SETUP, *errors)
    for label, env in (("test_cases", main_env), ("self_check", self_env)):
        if env["commit_id"] != pin:
            return fail_setup(PIN_MISMATCH, f"{label} interpreter has pic-standard "
                                            f"{env['commit_id']!r}, expected {pin}")
    try:
        du_path = Path(self_env["direct_url_path"]).resolve()
        du_before = sha256(du_path)
        out_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=out_root))
        work_root = Path(tempfile.mkdtemp(prefix="pic-vendor-"))
    except OSError as e:
        return fail_setup(INVALID_SETUP, f"cannot prepare run: {type(e).__name__}: {e}")

    # ---- protected execution ----
    checks: list[tuple] = []
    timed_out: list[str] = []
    termination: dict[str, str] = {}
    selfcheck_state = "not_started"
    error = None
    retain_work = False
    work = work_root / manifest["vendored_dir"]
    try:
        shutil.copytree(src, work)
        # The upstream copy ships an actual-results.json; require a fresh one.
        (work / "actual-results.json").unlink()
        cases = run_tree(python, ["test_cases.py"], work, a.timeout)
        save(out_dir, "test_cases", cases)
        if cases.timed_out:
            timed_out.append("test_cases")
            termination["test_cases"] = cases.termination
            retain_work = True
            checks.append(("test_cases completed within timeout", False, a.timeout))
        else:
            checks.append(("test_cases exit 0", cases.returncode == 0, cases.returncode))
            results_path = work / "actual-results.json"
            if results_path.is_file():
                shutil.copy2(results_path, out_dir / "actual-results.json")
                results = json.loads(results_path.read_text(encoding="utf-8"))
                checks += check_case_results(results, expected_ids, pin)
            else:
                checks.append(("fresh actual-results.json produced", False, None))

            # Fresh copy so self_check starts from upstream bytes.
            shutil.rmtree(work)
            shutil.copytree(src, work)
            selfcheck_state = "running"
            sc = run_tree(self_python, ["self_check.py"], work, a.timeout)
            selfcheck_state = "timed_out" if sc.timed_out else "completed"
            save(out_dir, "self_check", sc)
            if sc.timed_out:
                timed_out.append("self_check")
                termination["self_check"] = sc.termination
                retain_work = True
                checks.append(("self_check completed within timeout", False, a.timeout))
            else:
                checks.append(("self_check exit 0", sc.returncode == 0, sc.returncode))
                checks.append(check_self_check(sc.stdout))
    except BaseException as e:  # incl. KeyboardInterrupt: process-tree termination attempted
        error = f"{type(e).__name__}: {e}"
        retain_work = True
        checks.append(("runner completed without exception", False, error))

    # ---- final checks: each one independent of the others ----
    if not retain_work:
        shutil.rmtree(work_root, ignore_errors=True)
    try:
        restoration = check_restoration(selfcheck_state, du_path, du_before)
    except Exception as e:
        restoration = {"status": f"unverified ({type(e).__name__}: {e})",
                       "requires_recreation": True}
    checks.append(("self-check environment metadata restored",
                   restoration["status"] in ("untouched", "restored"),
                   restoration["status"]))
    try:
        _, after = verify_manifest(vendor)
    except Exception as e:
        after = [f"manifest verification raised {type(e).__name__}: {e}"]
    checks.append(("manifest intact after run", not after, after or None))

    passed = all(ok for _, ok, _ in checks)
    summary = {
        "upstream_commit": manifest["upstream_commit"],
        "pic_sdk_commit": pin,
        "test_cases_env": {k: main_env.get(k) for k in
                           ("python", "prefix", "commit_id", "direct_url_path")},
        "self_check_env": {k: self_env.get(k) for k in
                           ("python", "prefix", "commit_id", "direct_url_path")},
        "timeout_s": a.timeout,
        "timed_out": timed_out,
        "termination": termination,
        "error": error,
        "self_check_state": selfcheck_state,
        "self_check_env_restoration": restoration["status"],
        "self_check_env_requires_recreation": restoration["requires_recreation"],
        "work_dir_retained": str(work_root) if retain_work else None,
        "checks": [{"name": n, "ok": ok, "observed": obs} for n, ok, obs in checks],
        "passed": passed,
    }
    summary_written = True
    try:
        (out_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    except OSError as e:
        summary_written = False
        print(f"summary.json could not be written: {type(e).__name__}: {e}", file=sys.stderr)

    for name, ok, observed in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name} (observed: {observed})")
    if restoration["requires_recreation"]:
        print(f"self-check environment must be recreated: {self_env.get('prefix')}",
              file=sys.stderr)
    if retain_work:
        print(f"work tree retained, not deleted: {work_root}", file=sys.stderr)
    print(f"output: {out_dir}")

    if timed_out or error is not None or not summary_written:
        return INCOMPLETE
    return PASS if passed else CHECK_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
