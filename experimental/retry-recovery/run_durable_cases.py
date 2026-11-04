"""Produce D01 reproducible evidence from hard-exit subprocess scenarios."""
import argparse
import hashlib
import json
import platform
from pathlib import Path

from test_durable_restart import scenario_report

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    p = parser.parse_args()
    cases = scenario_report()
    assert len(cases) == 6 and all(x["passed"] for x in cases)
    report = {
        "python": platform.python_version(), "sqlite": __import__("sqlite3").sqlite_version,
        "tests": len(cases), "passed": len(cases), "cases": cases,
        "source_sha256": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                          for name in ("durable_restart.py", "test_durable_restart.py", "run_durable_cases.py")},
        "scope": "fresh Python worker processes, SQLite WAL+FULL app reservation and separate synthetic provider oracle; no live provider",
    }
    p.output.parent.mkdir(parents=True, exist_ok=True)
    p.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{len(cases)}/6 fresh-process hard-exit restart scenarios passed")


if __name__ == "__main__":
    main()
